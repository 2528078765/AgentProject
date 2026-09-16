"""用户自定义的付费 API 提供商（设置面板管理）。

存储分两处，刻意分开：
    config/providers.json   条目的非敏感部分（显示名、base_url、模型、协议模板）
                            —— 不含密钥，可以进 git、可以分享
    config/secrets.json     各条目的 API Key（该文件已在 .gitignore 里）

条目在编排层以 `voice:<id>` / `avatar:<id>` 的形式出现，和内置 provider
（edge_native / local_qwen_tts / local_wav2lip ...）并列，所以下拉框
和 provider 分发都能统一处理。
"""

from __future__ import annotations

import copy
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROVIDERS_PATH = Path("config/providers.json")
SECRETS_PATH = Path("config/secrets.json")

KINDS = ("voice", "avatar")

# 预设 → 协议模板。加一家新厂商 = 往这里加一份模板，不用改代码。
# 模板变量：{{api_key}} {{text}} {{voice_id}} {{model}} {{voice_name}}
#           {{audio_base64}} {{audio_format}} {{new_voice_id}} {{prompt_text}}
PRESETS: dict[str, dict[str, Any]] = {
    # ---------------------------------------------------------------- 语音
    "siliconflow": {
        "label": "硅基流动 CosyVoice2",
        "homepage": "https://cloud.siliconflow.cn/",
        "api_key_env": "AUTOVID_SILICONFLOW_KEY",
        "config": {
            # 只读鉴权检查，不提交语音合成任务，也不消耗生成额度。
            "connection_test": {
                "url": "https://api.siliconflow.cn/v1/batches?limit=1",
                "method": "GET",
                "headers": {"Authorization": "Bearer {{api_key}}"},
                "success_statuses": [200],
            },
            "url": "https://api.siliconflow.cn/v1/audio/speech",
            "method": "POST",
            "headers": {"Authorization": "Bearer {{api_key}}",
                        "Content-Type": "application/json"},
            "body": {"model": "{{model}}", "input": "{{text}}",
                     "voice": "{{voice_id}}", "response_format": "wav"},
            "model": "FunAudioLLM/CosyVoice2-0.5B",
            "audio_path": "",
            "audio_encoding": "raw",
            "timeout_s": 300,
            # 失败预算：连着被拒 6 次、或者这一句累计磕了 60 秒，就承认这家
            # 当前不可用，立刻抛出去让回退链接手。
            # 实测失败率在 25%~92% 之间剧烈波动（同一份配置、同一个音色），
            # 高失败率时以前会一路磕到十几分钟才回退 —— 现在几秒钟就换。
            "reject_streak_limit": 6,
            "reject_budget_s": 60,
            # ---- 能力声明：这家我方实测过（合成、上传、权限都验过）----
            "capabilities": {
                "verified": True,
                "source": ("实测：endpoint/模型/音色权限、wav 输出、"
                           "参考音频 ≤30s（超了报 audio longer than 30s）、"
                           "空 200 与近乎空 WAV 两种失败形态"),
                "output": {"width": 0, "height": 0, "note": "语音，无画面"},
                "audio": {"max_s": 0, "max_mb": 0, "formats": ["wav", "mp3", "pcm"],
                          "note": "参考音频（克隆用）≤ 30s，见 clone.max_audio_s"},
                "text": {"max_chars": 128000,
                         "note": "官方文档：input 长度 1~128000"},
            },
            # ---- 失败签名：这家会怎么拒绝 ----
            "failure_signatures": [
                {
                    "name": "sf_empty_200",
                    # 实测：失败请求 HTTP 200 + text/plain + 0 字节 + 0.14~0.24s 返回，
                    # 成功请求要 0.5~0.9s 才出首字节 —— 它根本没合成，是瞬间拒收。
                    # 而且失败请求**也带 x-siliconcloud-trace-id**，说明被平台受理过，
                    # 不是网关丢的。所以这是平台内部故障，不是我们参数写错。
                    "when": {"status": 200, "ctype_has": "text/plain", "max_bytes": 0},
                    "kind": "rejected",
                    "label": "硅基流动假装成功：HTTP 200 + text/plain + 0 字节",
                    "hint": ("这家把拒绝伪装成 200 且不给原因，重试是在硬磕。"
                             "响应头 x-siliconcloud-trace-id 是提工单的唯一凭据，"
                             "已记进日志。请换一家语音厂商。"),
                },
                {
                    "name": "sf_stub_wav",
                    # 实测第二种失败形态：HTTP 200 + **合法 audio/wav**，但只有
                    # 7854 字节 ≈ 0.16 秒（正常一句至少 24KB/0.5s）。
                    # 这类是「质量不合格」而不是「被拒绝」，重试是有意义的，
                    # 所以 kind=transient，行为不变 —— 只是让日志说清楚它是什么。
                    "when": {"status": 200, "ctype_has": "audio/wav", "max_bytes": 20000},
                    "kind": "transient",
                    "label": "硅基流动返回了合法但近乎空的 WAV（<0.2s）",
                    "hint": "这不是拒绝，是这次合成没出声。重试通常就好。",
                },
            ],
            "clone": {
                "enabled": True,
                # 硅基流动要求参考音频 ≤30 秒（实测报
                # "audio longer than 30s is not supported"），超了自动裁前 25 秒（贴边 30 会被判超标）。
                "max_audio_s": 25,
                # 第一步：multipart 上传参考音频，拿 uri
                "upload": {
                    "url": "https://api.siliconflow.cn/v1/uploads/audio/voice",
                    "mode": "multipart",
                    "headers": {"Authorization": "Bearer {{api_key}}"},
                    "fields": {"model": "{{model}}",
                               "customName": "{{voice_name}}",
                               "text": "{{prompt_text}}"},
                    "file_field": "file",
                    "filename": "ref.wav",
                    "file_ctype": "audio/wav",
                    "id_path": "uri",
                },
                # 第二步：这里其实不需要再建音色，uri 就是音色标识，
                # 所以 clone.url 留空 —— 由 _cloud_register_voice 的上传分支返回。
                "url": "",
            },
        },
    },
    "openai_compat": {
        "label": "OpenAI 兼容 TTS",
        "homepage": "https://platform.openai.com/docs/guides/text-to-speech",
        "api_key_env": "AUTOVID_OPENAI_TTS_KEY",
        "config": {
            "url": "https://api.openai.com/v1/audio/speech",
            "method": "POST",
            "headers": {"Authorization": "Bearer {{api_key}}",
                        "Content-Type": "application/json"},
            "body": {"model": "{{model}}", "input": "{{text}}",
                     "voice": "{{voice_id}}", "response_format": "wav"},
            "model": "tts-1",
            "audio_path": "",
            "audio_encoding": "raw",
            "timeout_s": 300,
            "clone": {"enabled": False},
        },
    },
    "http_json": {
        "label": "自建 GPT-SoVITS / CosyVoice",
        "homepage": "https://github.com/RVC-Boss/GPT-SoVITS",
        "api_key_env": "AUTOVID_HTTP_TTS_KEY",
        "config": {
            "url": "http://127.0.0.1:9880/tts",
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": {"text": "{{text}}", "text_lang": "zh",
                     "ref_audio_path": "{{ref_audio}}", "prompt_text": "{{prompt_text}}"},
            "audio_path": "",
            "audio_encoding": "raw",
            "timeout_s": 300,
            "clone": {"enabled": False},   # 自建服务通常每次带参考音频即克隆
        },
    },
    "minimax": {
        "label": "MiniMax 语音",
        "homepage": "https://platform.minimaxi.com/",
        "api_key_env": "AUTOVID_MINIMAX_KEY",
        "config": {
            "url": "https://api.minimaxi.com/v1/t2a_v2",
            "method": "POST",
            "headers": {"Authorization": "Bearer {{api_key}}",
                        "Content-Type": "application/json"},
            "body": {"model": "{{model}}", "text": "{{text}}",
                     "voice_setting": {"voice_id": "{{voice_id}}"}},
            "model": "speech-02-hd",
            "audio_path": "data.audio",
            "audio_encoding": "hex",
            "timeout_s": 300,
            "clone": {
                "enabled": True,
                "upload": {
                    "url": "https://api.minimaxi.com/v1/files/upload",
                    "mode": "multipart",
                    "headers": {"Authorization": "Bearer {{api_key}}"},
                    "fields": {"purpose": "voice_clone"},
                    "file_field": "file",
                    "filename": "ref.wav",
                    "file_ctype": "audio/wav",
                    "id_path": "file.file_id",
                },
                "url": "https://api.minimaxi.com/v1/voice_clone",
                "headers": {"Authorization": "Bearer {{api_key}}",
                            "Content-Type": "application/json"},
                "body": {"file_id": "{{upload_id}}", "voice_id": "{{new_voice_id}}"},
                "voice_id_path": "voice_id",
            },
        },
    },
    "elevenlabs": {
        "label": "ElevenLabs",
        "homepage": "https://elevenlabs.io/",
        "api_key_env": "AUTOVID_ELEVENLABS_KEY",
        "config": {
            "url": "https://api.elevenlabs.io/v1/text-to-speech/{{voice_id}}",
            "method": "POST",
            "headers": {"xi-api-key": "{{api_key}}",
                        "Content-Type": "application/json"},
            "body": {"text": "{{text}}", "model_id": "{{model}}"},
            "model": "eleven_multilingual_v2",
            "audio_path": "",
            "audio_encoding": "raw",
            "timeout_s": 300,
            "clone": {
                "enabled": True,
                "url": "https://api.elevenlabs.io/v1/voices/add",
                "headers": {"xi-api-key": "{{api_key}}"},
                "body": {"name": "{{voice_name}}", "files": "{{audio_base64}}"},
                "voice_id_path": "voice_id",
            },
        },
    },
    "fish_audio": {
        "label": "Fish Audio",
        "homepage": "https://fish.audio/",
        "api_key_env": "AUTOVID_FISH_KEY",
        "config": {
            "url": "https://api.fish.audio/v1/tts",
            "method": "POST",
            "headers": {"Authorization": "Bearer {{api_key}}",
                        "Content-Type": "application/json"},
            "body": {"text": "{{text}}", "reference_id": "{{voice_id}}",
                     "format": "wav"},
            "model": "s1",
            "audio_path": "",
            "audio_encoding": "raw",
            "timeout_s": 300,
            "clone": {"enabled": False},
        },
    },
    "dashscope": {
        "label": "阿里云百炼（CosyVoice 复刻）",
        "homepage": "https://bailian.console.aliyun.com/",
        "api_key_env": "AUTOVID_DASHSCOPE_KEY",
        "config": {
            "url": "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
                   "multimodal-generation/generation",
            "method": "POST",
            "headers": {"Authorization": "Bearer {{api_key}}",
                        "Content-Type": "application/json"},
            "body": {"model": "{{model}}", "input": {
                "text": "{{text}}",
                "voice": "{{voice_id}}"}},
            "model": "cosyvoice-clone-v1",
            "audio_path": "output.audio.url",
            "audio_encoding": "url",
            "timeout_s": 300,
            "clone": {"enabled": False},   # 音色复刻走控制台/独立接口
        },
    },
    "volcengine": {
        "label": "火山引擎（豆包语音）",
        "homepage": "https://www.volcengine.com/product/voice-tech",
        "api_key_env": "AUTOVID_VOLC_KEY",
        "config": {
            "url": "https://openspeech.bytedance.com/api/v1/tts",
            "method": "POST",
            "headers": {"Authorization": "Bearer;{{api_key}}",
                        "Content-Type": "application/json"},
            "body": {"app": {"appid": "{{model}}", "token": "{{api_key}}"},
                     "user": {"uid": "autovid"},
                     "audio": {"voice_type": "{{voice_id}}", "encoding": "wav"},
                     "request": {"text": "{{text}}", "operation": "query"}},
            "model": "",
            "audio_path": "data",
            "audio_encoding": "base64",
            "timeout_s": 300,
            "clone": {"enabled": False},
        },
    },
    # ---------------------------------------------------------------- 数字人
    # 数字人也走模板：提交 → 轮询 → 下载。
    # 变量：{{api_key}} {{model}} {{job_id}} {{image_base64}} {{image_data_uri}}
    #       {{audio_base64}} {{audio_data_uri}}
    "siliconflow_motion": {
        "label": "硅基流动 Wan2.2 自然动作 + 本地口型",
        "homepage": "https://cloud.siliconflow.cn/",
        "api_key_env": "AUTOVID_SILICONFLOW_KEY",
        "config": {
            "connection_test": {
                "url": "https://api.siliconflow.cn/v1/batches?limit=1",
                "method": "GET",
                "headers": {"Authorization": "Bearer {{api_key}}"},
                "success_statuses": [200],
            },
            "model": "Wan-AI/Wan2.2-I2V-A14B",
            "motion_prompt": (
                "画面中的单人讲解者坐在桌前面对镜头自然讲解，保持本人五官和服装，"
                "眼神看向镜头，自然眨眼和轻微点头，表情随讲解柔和变化，"
                "双手做幅度克制、符合日常聊天的解释手势，上半身稳定，"
                "固定机位，单人，真实摄影"
            ),
            "submit": {
                "url": "https://api.siliconflow.cn/v1/video/submit",
                "method": "POST",
                "headers": {"Authorization": "Bearer {{api_key}}",
                            "Content-Type": "application/json"},
                "body": {
                    "model": "{{model}}",
                    "prompt": "{{motion_prompt}}",
                    "negative_prompt": (
                        "夸张动作，快速挥手，手指畸形，多余手臂，多人，转身，"
                        "镜头移动，身份变化，脸部变形，卡通，低清晰度"
                    ),
                    "image_size": "720x1280",
                    "image": "{{image_data_uri}}",
                },
                "job_id_path": "requestId",
            },
            "query": {
                "url": "https://api.siliconflow.cn/v1/video/status",
                "method": "POST",
                "headers": {"Authorization": "Bearer {{api_key}}",
                            "Content-Type": "application/json"},
                "body": {"requestId": "{{job_id}}"},
                "status_path": "status",
                "done_values": ["Succeed"],
                "failed_values": ["Failed"],
                "result_path": "results.videos.0.url",
            },
            "postprocess": "local_wav2lip",
            "require_motion_duration_match": True,
            "use_scene_photo": True,
            "timeout_s": 1800,
            "poll_interval_s": 8,
            "merge_max_s": 30,
            "capabilities": {
                "verified": False,
                "source": "硅基流动官方 Wan2.2 I2V API + 项目本地 Wav2Lip 后处理",
                "output": {"width": 720, "height": 1280, "aspect": "9:16",
                           "note": "云端生成表情/头部/上半身/手势，本地按配音校正口型"},
                "image": {"formats": ["jpeg", "png"],
                          "note": "建议腰部以上、双手完整入镜的竖版单人照片"},
                "audio": {"max_s": 30,
                          "note": "音频不上传给 Wan2.2，仅用于本地口型后处理"},
            },
        },
    },
    "d_id": {
        "label": "D-ID",
        "homepage": "https://www.d-id.com/",
        "api_key_env": "AUTOVID_DID_KEY",
        "config": {
            "model": "",
            # D-ID **只收公网 URL**（实测 data URI 直接 400：
            # "must be a valid image URL (ending with jpg|jpeg|png)"），
            # 所以先走它自己的资产上传接口拿 URL，再提交。
            "upload": {
                "image": {
                    "url": "https://api.d-id.com/images",
                    "field": "image",
                    "filename": "autovid.jpg",
                    "content_type": "image/jpeg",
                    "url_path": "url",
                    # 实测踩坑：不显式要求检测时，响应里的 faces 字段会是 **null**，
                    # 于是我们的人脸闸门会被静默跳过（以为查过了，其实没查）。
                    # 显式传 detect_faces 才稳定拿到 faces[]。
                    "fields": {"detect_faces": "true"},
                    "headers": {"Authorization": "Basic {{api_key}}"},
                },
                "audio": {
                    "url": "https://api.d-id.com/audios",
                    "field": "audio",
                    "filename": "autovid.wav",
                    "content_type": "audio/wav",
                    "url_path": "url",
                    "headers": {"Authorization": "Basic {{api_key}}"},
                },
            },
            "submit": {
                "url": "https://api.d-id.com/talks",
                "method": "POST",
                "headers": {"Authorization": "Basic {{api_key}}",
                            "Content-Type": "application/json"},
                "body": {
                    "source_url": "{{image_url}}",
                    "script": {"type": "audio", "audio_url": "{{audio_url}}"},
                },
                "job_id_path": "id",
            },
            "query": {
                "url": "https://api.d-id.com/talks/{{job_id}}",
                "method": "GET",
                "headers": {"Authorization": "Basic {{api_key}}"},
                "status_path": "status",
                "done_values": ["done"],
                "failed_values": ["error", "rejected", "failed"],
                "result_path": "result_url",
            },
            "timeout_s": 1800,
            "poll_interval_s": 10,
            # 逐句提交就是几十次上传 + 几十次轮询，任何一次抖动都会毁掉整条链路
            # （实测第 3 次轮询 SSL 断流直接中止），而且按次计费很亏（实测一次
            # 音频只有 1 秒的任务也扣 1 个额度，与长短基本无关）。合并成 1 个任务。
            "merge_max_s": 150,
            # ---- 能力声明：输出与图片限制是**我方实测**，音频限制来自官方 spec ----
            "capabilities": {
                "verified": True,
                "source": ("输出规格 512×512@25fps 与图片限制（160 最小边、"
                           "10×1024×1024 像素上限、上传不校验人脸）为实测；"
                           "音频 15MB/10 分钟来自官方 OpenAPI spec，未实测"),
                "output": {"width": 512, "height": 512, "fps": 25, "aspect": "1:1",
                           "note": "照片数字人只出方片；要更大需要 config.stitch（未实测）"},
                # spec: "File size is limited to 15MB. Audio length is limited
                # 5 minutes for clips and 10 minutes for talks."
                "audio": {"max_s": 600, "max_mb": 15,
                          "formats": ["wav", "mp3", "mp4", "m4a", "flac"],
                          "note": "/audios 上传后会被转成 16kHz WAV"},
                # 实测：128×128 / 64×64 -> 400 InvalidImageResolutionError
                #       256×256 通过；3072×4096 -> 201 上传通过、/talks 才报
                #       InvalidFileSizeError（阈值 = 10*1024*1024 像素）
                "image": {"min_side": 160, "max_pixels": 10485760,
                          "formats": ["jpeg", "png"], "reports_faces": True,
                          "note": "上传不校验像素上限与是否有人脸，提交任务时才爆"},
            },
            # ---- 失败签名：全部为实测/官方 spec ----
            "failure_signatures": [
                {
                    "name": "did_image_too_large",
                    # 实测最阴的一条：/images 上传 3072×4096 返回 201 成功，
                    # 到 /talks 才报 "file size exceeded 10 MB" —— 而那张 JPEG
                    # 只有 738KB。错误信息完全是误导的，真正的规则是像素总数。
                    "when": {"status_in": [400], "body_key": {"key": "kind",
                                                              "equals": "InvalidFileSizeError"}},
                    "kind": "rejected",
                    "label": "D-ID 拒收：InvalidFileSizeError",
                    "hint": ("注意这条几乎总是**图片像素超限**，不是文件体积超限 —— "
                             "实测 2304×3072(7.1Mpx) 通过、3072×4096(12.6Mpx) 失败，"
                             "阈值就是 10×1024×1024 像素。而上传阶段不查，提交才报。"),
                },
                {
                    "name": "did_image_too_small",
                    "when": {"status_in": [400],
                             "body_key": {"key": "kind",
                                          "equals": "InvalidImageResolutionError"}},
                    "kind": "rejected",
                    "label": "D-ID 拒收：图片分辨率太低",
                    "hint": "实测门槛是 160×160。别靠放大糊过去，换一张清晰的照片。",
                },
                {
                    "name": "did_moderation",
                    "when": {"status_in": [451]},
                    "kind": "rejected",
                    "label": "D-ID 内容审核拦截（HTTP 451）",
                    "hint": ("可能是 ImageModerationError / CelebrityRecognizedError / "
                             "TextModerationError / AudioModerationError。换素材，"
                             "或联系 D-ID 走人工复核。"),
                },
                {
                    "name": "did_conflict_request",
                    "when": {"status_in": [409]},
                    "kind": "transient",
                    "label": "D-ID 同一请求正在处理中（HTTP 409）",
                },
            ],
        },
    },
    "heygen": {
        "label": "HeyGen",
        "homepage": "https://www.heygen.com/",
        "api_key_env": "AUTOVID_HEYGEN_KEY",
        "config": {
            "model": "",
            "submit": {
                "url": "https://api.heygen.com/v2/video/generate",
                "method": "POST",
                "headers": {"X-Api-Key": "{{api_key}}",
                            "Content-Type": "application/json"},
                "body": {
                    "video_inputs": [{
                        "character": {"type": "talking_photo",
                                      "talking_photo": {"type": "url",
                                                        "url": "{{image_data_uri}}"}},
                        "voice": {"type": "audio", "audio_url": "{{audio_data_uri}}"},
                    }],
                    "dimension": {"width": 1080, "height": 1920},
                },
                "job_id_path": "data.video_id",
            },
            "query": {
                "url": "https://api.heygen.com/v1/video_status.get?video_id={{job_id}}",
                "method": "GET",
                "headers": {"X-Api-Key": "{{api_key}}"},
                "status_path": "data.status",
                "done_values": ["completed"],
                "failed_values": ["failed"],
                "result_path": "data.video_url",
            },
            "timeout_s": 1800,
            "poll_interval_s": 10,
            # ---- 能力声明：**文档值，我方未实测** ----
            # 关键差异：HeyGen 的 dimension 是「你说了算」，可以原生出 9:16 1080×1920；
            # D-ID 的照片数字人只能出 512×512 方片。同样叫「数字人」，
            # 两家的天花板差了 7.9 倍 —— 这就是能力声明必须写进配置的理由。
            "capabilities": {
                "verified": False,
                "source": "HeyGen 文档（第三方整理，未实测）",
                "output": {"width": 1080, "height": 1920, "aspect": "9:16",
                           "note": "支持 720p/1080p 的 16:9 / 9:16 / 1:1；"
                                   "也可自定义，须为偶数且 128~4096"},
                "image": {"min_side": 128, "max_side": 4096,
                          "formats": ["jpeg", "png"],
                          "note": "自定义尺寸 128~4096 且必须为偶数"},
                "audio": {"note": "音频时长上限未核实，暂不断言"},
            },
            "failure_signatures": [
                {"name": "heygen_auth",
                 "when": {"status_in": [401, 403]},
                 "kind": "fatal",
                 "label": "HeyGen 认证/权限被拒",
                 "hint": "X-Api-Key 无效，或该功能未开通（talking_photo 可能要特定套餐）。"},
                {"name": "heygen_quota",
                 "when": {"status_in": [402]},
                 "kind": "fatal",
                 "label": "HeyGen 额度/计费被拒",
                 "hint": "充值或换一家。重试没有意义。"},
            ],
        },
    },
    "minimax_h3": {
        "label": "MiniMax 海螺视频",
        "homepage": "https://platform.minimaxi.com/",
        "api_key_env": "AUTOVID_MINIMAX_API_KEY",
        "config": {
            "model": "MiniMax-Hailuo-H3",
            "submit": {
                "url": "https://api.minimaxi.com/v1/video_generation",
                "method": "POST",
                "headers": {"Authorization": "Bearer {{api_key}}",
                            "Content-Type": "application/json"},
                "body": {"model": "{{model}}",
                         "first_frame_image": "{{image_data_uri}}",
                         "audio_url": "{{audio_data_uri}}"},
                "job_id_path": "task_id",
            },
            "query": {
                "url": "https://api.minimaxi.com/v1/query/video_generation?task_id={{job_id}}",
                "method": "GET",
                "headers": {"Authorization": "Bearer {{api_key}}"},
                "status_path": "status",
                "done_values": ["Success", "success"],
                "failed_values": ["Fail", "fail"],
                "result_path": "file_id",
            },
            "timeout_s": 1800,
            "poll_interval_s": 10,
        },
    },
    "dashscope_s2v": {
        "label": "阿里云百炼 Wan2.2-S2V（自然讲解）",
        "homepage": "https://bailian.console.aliyun.com/",
        "key_url": "https://bailian.console.aliyun.com/",
        "api_key_env": "AUTOVID_DASHSCOPE_KEY",
        # 设置页按元数据渲染厂商专属字段，避免在前端硬编码某一家。
        "fields": [
            {
                "name": "workspace_id",
                "label": "业务空间 ID",
                "placeholder": "例如 llm-xxxxxxxx",
                "required": True,
                "hint": "在百炼控制台的业务空间详情中查看，需使用北京地域。",
            },
        ],
        "config": {
            "workspace_id": "",
            "model": "wan2.2-s2v",
            # 获取临时上传凭证是只读操作，可验证 Key 且不会创建视频任务。
            "connection_test": {
                # 不只验证 Key：直接查当前业务空间是否真的授权了这个模型。
                # 之前只取上传凭证会把“Key 有效但模型未授权”误报成连通成功。
                "url": ("https://{{workspace_id}}.cn-beijing.maas.aliyuncs.com/"
                        "api/v1/models/permissions?model={{model}}&"
                        "authorization_scope=AUTHORIZED&action=INFERENCE&"
                        "page_no=1&page_size=20"),
                "method": "GET",
                "headers": {"Authorization": "Bearer {{api_key}}"},
                "success_statuses": [200],
                "json_assert": {
                    "path": "output.permissions.0.permissions.inference",
                    "equals": True,
                },
                "failure_message": ("Key 和业务空间可以连接，但 Wan2.2-S2V 当前不可调用。"
                                    "请检查账户欠费、百炼服务开通状态及当前空间的模型授权。"),
                "success_message": "连接成功，Key、业务空间和模型权限均有效",
            },
            "upload": {
                "image": {
                    "mode": "dashscope_oss",
                    "policy_url": ("https://dashscope.aliyuncs.com/api/v1/uploads"
                                   "?action=getPolicy&model={{model}}"),
                    "filename": "autovid.jpg",
                    "content_type": "image/jpeg",
                },
                "audio": {
                    "mode": "dashscope_oss",
                    "policy_url": ("https://dashscope.aliyuncs.com/api/v1/uploads"
                                   "?action=getPolicy&model={{model}}"),
                    "filename": "autovid.wav",
                    "content_type": "audio/wav",
                },
            },
            "submit": {
                "url": ("https://{{workspace_id}}.cn-beijing.maas.aliyuncs.com/"
                        "api/v1/services/aigc/image2video/video-synthesis"),
                "method": "POST",
                "headers": {
                    "Authorization": "Bearer {{api_key}}",
                    "Content-Type": "application/json",
                    "X-DashScope-Async": "enable",
                    "X-DashScope-OssResourceResolve": "enable",
                },
                "body": {
                    "model": "{{model}}",
                    "input": {
                        "image_url": "{{image_url}}",
                        "audio_url": "{{audio_url}}",
                    },
                    "parameters": {"resolution": "480P"},
                },
                "job_id_path": "output.task_id",
            },
            "query": {
                "url": ("https://{{workspace_id}}.cn-beijing.maas.aliyuncs.com/"
                        "api/v1/tasks/{{job_id}}"),
                "method": "GET",
                "headers": {"Authorization": "Bearer {{api_key}}"},
                "status_path": "output.task_status",
                "done_values": ["SUCCEEDED"],
                "failed_values": ["FAILED", "UNKNOWN", "CANCELED"],
                "result_path": "output.results.video_url",
            },
            "use_scene_photo": True,
            "timeout_s": 1800,
            "poll_interval_s": 10,
            # 官方单次音频需小于 20 秒，工作流按 19 秒拆分后再合并。
            "merge_max_s": 19,
            "capabilities": {
                "verified": False,
                "source": "阿里云百炼 Wan2.2-S2V 官方 API 文档",
                "output": {"width": 480, "height": 854, "aspect": "9:16",
                           "note": "480P 档，由图片和音频共同驱动口型、表情与动作"},
                "image": {"min_side": 400, "formats": ["jpeg", "png", "webp"],
                          "note": "建议单人、腰部以上、正脸清晰、双手完整入镜"},
                "audio": {"max_s": 19, "max_mb": 15,
                          "formats": ["wav", "mp3"],
                          "note": "单次小于 20 秒，长口播由工作流自动拆分"},
            },
            "failure_signatures": [
                {"name": "dashscope_unpurchased",
                 "when": {"status_in": [403], "body_has": "AccessDenied.Unpurchased"},
                 "kind": "fatal", "label": "阿里云百炼服务或模型当前不可用",
                 "hint": ("检查账户是否欠费、百炼是否开通，以及当前业务空间是否获得 "
                          "Wan2.2-S2V 推理权限。你的账户若为负余额，需先结清欠费。")},
                {"name": "dashscope_model_access",
                 "when": {"status_in": [403], "body_has": "Model.AccessDenied"},
                 "kind": "fatal", "label": "当前业务空间没有模型调用权限",
                 "hint": "请在百炼控制台为当前业务空间授权 Wan2.2-S2V 推理权限。"},
                {"name": "dashscope_free_tier_only",
                 "when": {"status_in": [403], "body_has": "AllocationQuota.FreeTierOnly"},
                 "kind": "fatal", "label": "免费额度已耗尽且已开启用完即停",
                 "hint": "在百炼控制台查看免费额度，或关闭用完即停并确认按量付费。"},
                {"name": "dashscope_quota",
                 "when": {"status_in": [400, 402, 403], "body_has": "Arrearage"},
                 "kind": "fatal", "label": "阿里云百炼余额或免费额度不足",
                 "hint": "前往百炼控制台查看免费额度或充值。"},
                {"name": "dashscope_auth", "when": {"status_in": [401]},
                 "kind": "fatal", "label": "阿里云百炼 API Key 无效",
                 "hint": "检查 Key 是否属于北京地域和当前业务空间。"},
                {"name": "dashscope_permission", "when": {"status_in": [403]},
                 "kind": "fatal", "label": "阿里云百炼权限被拒",
                 "hint": "检查业务空间 ID、地域和模型调用权限。"},
            ],
        },
    },
    "fal": {
        "label": "fal.ai",
        "homepage": "https://fal.ai/",
        "api_key_env": "AUTOVID_FAL_KEY",
        "config": {
            "model": "fal-ai/sync-lipsync",
            # 官方模型列表是只读鉴权接口，不会创建任务或产生生成费用。
            "connection_test": {
                "url": "https://api.fal.ai/v1/models?limit=1",
                "method": "GET",
                "headers": {"Authorization": "Key {{api_key}}"},
                "success_statuses": [200],
            },
            "submit": {
                "url": "https://queue.fal.run/{{model}}",
                "method": "POST",
                "headers": {"Authorization": "Key {{api_key}}",
                            "Content-Type": "application/json"},
                "body": {"image_url": "{{image_data_uri}}",
                         "audio_url": "{{audio_data_uri}}"},
                "job_id_path": "request_id",
            },
            "query": {
                "url": "https://queue.fal.run/{{model}}/requests/{{job_id}}/status",
                "method": "GET",
                "headers": {"Authorization": "Key {{api_key}}"},
                "url_path": "status_url",
                "status_path": "status",
                "done_values": ["COMPLETED", "completed"],
                "failed_values": ["FAILED", "failed"],
            },
            "result": {
                "url": "https://queue.fal.run/{{model}}/requests/{{job_id}}",
                "method": "GET",
                "headers": {"Authorization": "Key {{api_key}}"},
                "url_path": "response_url",
                "result_path": "video.url",
            },
            "timeout_s": 1800,
            "poll_interval_s": 5,
        },
    },
    "fal_omnihuman": {
        "label": "fal.ai OmniHuman 1.5（自然讲解）",
        "homepage": "https://fal.ai/models/fal-ai/bytedance/omnihuman/v1.5",
        "key_url": "https://fal.ai/dashboard/keys",
        "api_key_env": "AUTOVID_FAL_KEY",
        "config": {
            "model": "fal-ai/bytedance/omnihuman/v1.5",
            "connection_test": {
                "url": "https://api.fal.ai/v1/models?limit=1",
                "method": "GET",
                "headers": {"Authorization": "Key {{api_key}}"},
                "success_statuses": [200],
            },
            "motion_prompt": (
                "A single presenter speaks naturally to the camera in a calm explanatory tone. "
                "Keep the person's identity, clothing and background stable. Use subtle facial "
                "expressions, natural blinking, small head movements and restrained conversational "
                "hand gestures that follow the rhythm and meaning of the speech. Static camera."
            ),
            "submit": {
                "url": "https://queue.fal.run/{{model}}",
                "method": "POST",
                "headers": {"Authorization": "Key {{api_key}}",
                            "Content-Type": "application/json"},
                "body": {
                    "image_url": "{{image_data_uri}}",
                    "audio_url": "{{audio_data_uri}}",
                    "prompt": "{{motion_prompt}}",
                    "resolution": "720p",
                    "turbo_mode": False,
                },
                "job_id_path": "request_id",
            },
            "query": {
                "url": "https://queue.fal.run/{{model}}/requests/{{job_id}}/status",
                "method": "GET",
                "headers": {"Authorization": "Key {{api_key}}"},
                "url_path": "status_url",
                "status_path": "status",
                "done_values": ["COMPLETED"],
                "failed_values": ["FAILED"],
            },
            "result": {
                "url": "https://queue.fal.run/{{model}}/requests/{{job_id}}",
                "method": "GET",
                "headers": {"Authorization": "Key {{api_key}}"},
                "url_path": "response_url",
                "result_path": "video.url",
            },
            "use_scene_photo": True,
            "timeout_s": 3600,
            "poll_interval_s": 8,
            "merge_max_s": 59,
            "capabilities": {
                "verified": False,
                "source": "fal.ai 官方 OmniHuman 1.5 API 文档",
                "output": {"width": 720, "height": 1280, "aspect": "9:16",
                           "note": "人物动作与表情由图片和配音共同驱动，不再循环 5 秒动作片"},
                "image": {"formats": ["jpeg", "png", "webp"],
                          "note": "建议单人、腰部以上、双手完整入镜"},
                "audio": {"max_s": 60,
                          "note": "720p 最长 60 秒；动作和口型都直接跟随音频"},
            },
            "failure_signatures": [
                {"name": "fal_balance_exhausted",
                 "when": {"status_in": [402, 403], "body_has": "Exhausted balance"},
                 "kind": "fatal", "label": "fal.ai 余额耗尽，账户已锁定",
                 "hint": "前往账单页充值，或换一家数字人提供商。"},
                {"name": "fal_auth", "when": {"status_in": [401]},
                 "kind": "fatal", "label": "fal.ai 认证失败",
                 "hint": "检查 FAL_KEY 或设置页里的 API Key。"},
                {"name": "fal_quota", "when": {"status_in": [402]},
                 "kind": "fatal", "label": "fal.ai 额度不足",
                 "hint": "账户余额不足，充值后再试。"},
                {"name": "fal_permission", "when": {"status_in": [403]},
                 "kind": "fatal", "label": "fal.ai 权限被拒",
                 "hint": "Key 有效，但当前账户或模型没有调用权限。"},
            ],
        },
    },
    "replicate": {
        "label": "Replicate",
        "homepage": "https://replicate.com/",
        "api_key_env": "AUTOVID_REPLICATE_KEY",
        "config": {
            "model": "",
            "submit": {
                "url": "https://api.replicate.com/v1/predictions",
                "method": "POST",
                "headers": {"Authorization": "Bearer {{api_key}}",
                            "Content-Type": "application/json"},
                "body": {"input": {"face": "{{image_data_uri}}",
                                   "audio": "{{audio_data_uri}}"}},
                "job_id_path": "id",
            },
            "query": {
                "url": "https://api.replicate.com/v1/predictions/{{job_id}}",
                "method": "GET",
                "headers": {"Authorization": "Bearer {{api_key}}"},
                "status_path": "status",
                "done_values": ["succeeded"],
                "failed_values": ["failed", "canceled"],
                "result_path": "output",
            },
            "timeout_s": 1800,
            "poll_interval_s": 5,
        },
    },
    "custom_http_job": {
        "label": "通用「提交 + 轮询」服务",
        "homepage": "",
        "api_key_env": "AUTOVID_AVATAR_KEY",
        "config": {
            "model": "",
            "submit": {
                "url": "",
                "method": "POST",
                "headers": {"Authorization": "Bearer {{api_key}}",
                            "Content-Type": "application/json"},
                "body": {"image": "{{image_data_uri}}",
                         "audio": "{{audio_data_uri}}"},
                "job_id_path": "id",
            },
            "query": {
                "url": "",
                "method": "GET",
                "headers": {"Authorization": "Bearer {{api_key}}"},
                "status_path": "status",
                "done_values": ["done", "success", "succeeded", "completed"],
                "failed_values": ["error", "failed", "rejected"],
                "result_path": "video_url",
            },
            "timeout_s": 1800,
            "poll_interval_s": 10,
        },
    },
}


# 设置页只让用户选择“供应商”，模型放在下一层选择。
# 旧版把 fal.ai 通用模板与 OmniHuman 模板并排展示，用户无法判断两者差异；
# 新版统一为一个 fal.ai，并以符合本项目“自然讲解”目标的 OmniHuman 为默认协议。
# fal_omnihuman 仍保留用于兼容历史配置，但不再出现在新增下拉框中。
PRESETS["fal"] = copy.deepcopy(PRESETS["fal_omnihuman"])
PRESETS["fal"]["label"] = "fal.ai"
PRESETS["fal"]["homepage"] = "https://fal.ai/models"
PRESETS["fal_omnihuman"]["hidden"] = True


# 供应商在设置页的展示信息。模型列表只包含这套工作流已经接好协议的模型，
# 不冒充厂商的完整模型市场；以后新增适配模型只需在这里追加选项。
PRESET_UI: dict[str, dict[str, Any]] = {
    "siliconflow": {
        "label": "硅基流动",
        "models": [{"id": "FunAudioLLM/CosyVoice2-0.5B", "label": "CosyVoice2 音色克隆"}],
    },
    "openai_compat": {
        "label": "OpenAI",
        "models": [{"id": "tts-1", "label": "TTS 1"}],
    },
    "http_json": {"label": "自建语音服务", "editable_label": True,
                  "editable_endpoint": True, "editable_model": True},
    "minimax": {
        "label": "MiniMax",
        "models": [{"id": "speech-02-hd", "label": "Speech 02 HD"}],
    },
    "elevenlabs": {
        "label": "ElevenLabs",
        "models": [{"id": "eleven_multilingual_v2", "label": "Multilingual V2"}],
    },
    "fish_audio": {
        "label": "Fish Audio",
        "models": [{"id": "s1", "label": "S1"}],
    },
    "dashscope": {
        "label": "阿里云百炼",
        "models": [{"id": "cosyvoice-clone-v1", "label": "CosyVoice 音色复刻"}],
    },
    "volcengine": {"label": "火山引擎", "editable_model": True},
    "siliconflow_motion": {
        "label": "硅基流动",
        "models": [{"id": "Wan-AI/Wan2.2-I2V-A14B", "label": "Wan2.2 自然动作"}],
    },
    "d_id": {"label": "D-ID"},
    "heygen": {"label": "HeyGen"},
    "minimax_h3": {
        "label": "MiniMax",
        "models": [{"id": "MiniMax-Hailuo-H3", "label": "海螺 H3"}],
    },
    "dashscope_s2v": {
        "label": "阿里云百炼",
        "models": [{"id": "wan2.2-s2v", "label": "Wan2.2-S2V 自然讲解"}],
    },
    "fal": {
        "label": "fal.ai",
        "models": [{"id": "fal-ai/bytedance/omnihuman/v1.5",
                    "label": "OmniHuman 1.5 自然讲解"}],
    },
    "replicate": {"label": "Replicate", "editable_model": True},
    "custom_http_job": {"label": "自定义服务", "editable_label": True,
                        "editable_endpoint": True, "editable_model": True},
}


# --------------------------------------------------------------------------- #
# 预设的元信息：用途 + 能力声明
# --------------------------------------------------------------------------- #
# 「预设属于语音还是数字人」以前只存在于调用方的参数里，预设本身不记。
# 结果是设置面板两个下拉框都能选到全部预设，「任意一家」变成了「随便选一个，
# 选错了再报错」。这里把它变成预设自己的属性。
PRESET_KINDS: dict[str, str] = {
    "siliconflow": "voice", "openai_compat": "voice", "http_json": "voice",
    "minimax": "voice", "elevenlabs": "voice", "fish_audio": "voice",
    "dashscope": "voice", "volcengine": "voice",
    "siliconflow_motion": "avatar", "d_id": "avatar", "heygen": "avatar", "minimax_h3": "avatar",
    "dashscope_s2v": "avatar", "fal": "avatar", "fal_omnihuman": "avatar",
    "replicate": "avatar",
    "custom_http_job": "avatar",
}

# 没声明能力的预设，明确写下「我不知道」，而不是留空。
# 留空和「无限制」在数据上长得一样（都是 0），但含义相反：
# 一个是「没查过」，一个是「确认没有」。混淆这两者正是静默失败的温床。
_UNKNOWN_NOTE = "尚未核实（输出分辨率、图片与音频限制都没验过）"


def _normalize_presets() -> None:
    for name, preset in PRESETS.items():
        preset.setdefault("kind", PRESET_KINDS.get(name, "avatar"))
        cfg = preset.setdefault("config", {})
        caps = cfg.setdefault("capabilities", {})
        caps.setdefault("verified", False)
        if not caps.get("source"):
            caps["source"] = "未核实"
        # 只补「完全没说」的那一项，已经写了的（哪怕是部分写的）不动
        if preset["kind"] == "avatar":
            caps.setdefault("output", {"note": _UNKNOWN_NOTE})
        else:
            caps.setdefault("output", {"note": "语音，无画面"})
        caps.setdefault("image", {"note": _UNKNOWN_NOTE if preset["kind"] == "avatar"
                                  else "语音，不吃图"})
        caps.setdefault("audio", {"note": _UNKNOWN_NOTE})


_normalize_presets()


def preset_kind(name: str) -> str:
    """这个预设是语音还是数字人。"""
    return PRESET_KINDS.get(name, str((PRESETS.get(name) or {}).get("kind") or ""))


def capability_note(name: str) -> str:
    """这家声明了什么，以及这些数字可不可信。设置面板和预检都用它。"""
    from . import provider_caps as _caps  # noqa: PLC0415

    preset = PRESETS.get(name)
    if preset is None:
        return ""
    return _caps.capability_summary(preset.get("config") or {})


def _path(config: Any, relative: Path) -> Path:
    return config.path(str(relative))


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


# --------------------------------------------------------------------------- #
# 条目
# --------------------------------------------------------------------------- #
def load_entries(config: Any) -> list[dict[str, Any]]:
    data = _read_json(_path(config, PROVIDERS_PATH), {"providers": []})
    entries = data.get("providers") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    out = []
    for item in entries:
        if isinstance(item, dict) and item.get("id"):
            item = dict(item)
            # 旧版 fal.ai OmniHuman 是独立模板；设置页现在统一显示为 fal.ai。
            if item.get("preset") == "fal_omnihuman":
                item["preset"] = "fal"
            # 官方模板统一显示平台名称，具体模型在模型下拉框里表达。
            # 自建服务仍允许用户自己命名。
            ui = PRESET_UI.get(str(item.get("preset") or "")) or {}
            if ui.get("label") and not ui.get("editable_label"):
                item["label"] = str(ui["label"])
            item["has_key"] = bool(get_key(config, str(item["id"])))
            out.append(item)
    return out


def save_entries(config: Any, entries: list[dict[str, Any]]) -> None:
    clean = []
    for item in entries:
        copy = {k: v for k, v in item.items() if k != "has_key"}
        clean.append(copy)
    _write_json(_path(config, PROVIDERS_PATH), {"providers": clean})


def make_id(kind: str, label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-")
    if not slug:
        slug = time.strftime("%H%M%S")
    return f"{kind}-{slug}"[:60]


def upsert_entry(config: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """新增或更新一个提供商条目；密钥单独存。"""
    kind = str(entry.get("kind") or "").strip()
    if kind not in KINDS:
        raise ValueError(f"kind 必须是 {KINDS} 之一，收到 {kind!r}")
    label = str(entry.get("label") or "").strip()
    if not label:
        raise ValueError("提供商名称不能为空")

    preset = str(entry.get("preset") or "").strip()
    template = PRESETS.get(preset, {}).get("config")
    entries = load_entries(config)
    entry_id = str(entry.get("id") or "").strip()

    existing = next((e for e in entries if e["id"] == entry_id), None) if entry_id else None
    # 没给 id 时，按「同用途 + 同名称」认作同一个条目去更新，
    # 否则反复保存会在文件里堆出一串同 id 的重复项（实测踩过）。
    if existing is None:
        existing = next((e for e in entries
                         if e.get("kind") == kind and e.get("label") == label), None)
    if existing is None:
        entry_id = entry_id or make_id(kind, label)
        existing = {"id": entry_id}
        entries.append(existing)
    else:
        entry_id = str(existing["id"])

    # 配置：预设模板 → 已被用户改过的旧值 → 本次提交的覆盖
    # 下面会按用户填写的 base_url 改写嵌套 URL；必须深拷贝，否则一次编辑会
    # 污染全局预设，连带改变同进程里其他提供商的地址。
    base = copy.deepcopy(template or {})
    old = existing.get("config")
    if isinstance(old, dict):
        base.update(old)
    incoming = entry.get("config")
    if isinstance(incoming, dict):
        base.update({k: v for k, v in incoming.items() if v not in (None, "")})

    existing.update({
        "id": entry_id,
        "kind": kind,
        "label": label,
        "preset": preset,
        "enabled": bool(entry.get("enabled", True)),
        "config": base,
    })
    # 直接可编辑的常用字段
    for field in ("base_url", "model", "voice_id", "protocol_note"):
        if field in entry:
            existing[field] = entry[field]
    if entry.get("base_url"):
        _apply_base_url(base, kind, str(entry["base_url"]))
    if entry.get("model"):
        base["model"] = entry["model"]
    if entry.get("voice_id"):
        base["voice_id"] = entry["voice_id"]

    save_entries(config, entries)
    api_key = str(entry.get("api_key") or "").strip()
    if api_key:
        set_key(config, entry_id, api_key)
    existing["has_key"] = bool(get_key(config, entry_id))
    return existing


def _apply_base_url(cfg: dict[str, Any], kind: str, base_url: str) -> None:
    """把用户填的 base_url 套到模板里的各个 URL 上。

    坑：模板 URL 是 `https://api.xxx.cn/v1/audio/speech`，用户填的 base_url
    往往也带 `/v1`。如果只把「scheme://host」换掉，就会拼成
    `/v1/v1/audio/speech`（实测踩过）。
    正确做法：以 base_url 的 path 为基准，把模板 path 里重复的那一段去掉。
    """
    base = base_url.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    base_path = parsed.path.rstrip("/")          # 例如 "/v1"

    def rewrite(url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        path = parts.path
        if base_path and path.startswith(base_path):
            path = path[len(base_path):]
        return base + path + (("?" + parts.query) if parts.query else "")

    connection_test = cfg.get("connection_test")
    if isinstance(connection_test, dict) and connection_test.get("url"):
        connection_test["url"] = rewrite(str(connection_test["url"]))

    if kind == "voice":
        if cfg.get("url"):
            cfg["url"] = rewrite(str(cfg["url"]))
        clone = cfg.get("clone") or {}
        upload = clone.get("upload") or {}
        if upload.get("url"):
            upload["url"] = rewrite(str(upload["url"]))
        if clone.get("url"):
            clone["url"] = rewrite(str(clone["url"]))
    else:
        # 数字人用 submit.url / query.url（和语音统一的模板结构）
        for section in ("submit", "query"):
            block = cfg.get(section)
            if isinstance(block, dict) and block.get("url"):
                block["url"] = rewrite(str(block["url"]))
        # 兼容老的扁平字段
        for key in ("submit_url", "query_url", "base_url"):
            if cfg.get(key):
                cfg[key] = rewrite(str(cfg[key]))


def delete_entry(config: Any, entry_id: str) -> bool:
    entries = load_entries(config)
    keep = [e for e in entries if e["id"] != entry_id]
    if len(keep) == len(entries):
        return False
    save_entries(config, keep)
    set_key(config, entry_id, "")
    return True


def get_entry(config: Any, entry_id: str) -> dict[str, Any] | None:
    return next((e for e in load_entries(config) if e["id"] == entry_id), None)


# --------------------------------------------------------------------------- #
# 连通测试
# --------------------------------------------------------------------------- #
def _test_template(value: Any, variables: dict[str, str]) -> Any:
    """替换连通测试所需的少量模板变量，不把密钥写回配置或日志。"""
    if isinstance(value, str):
        for name, replacement in variables.items():
            value = value.replace("{{" + name + "}}", replacement)
        return value
    if isinstance(value, dict):
        return {str(k): _test_template(v, variables) for k, v in value.items()}
    return value


def _test_dig(data: Any, path: str) -> Any:
    """读取连通探测响应中的点路径，支持数组下标。"""
    node = data
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
        if node is None:
            return None
    return node


def _safe_probe_from_config(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """为没有专用检查接口的模板取一个只读、不会创建任务的地址。"""
    candidates = [cfg.get("url"), cfg.get("base_url"), cfg.get("submit_url")]
    submit = cfg.get("submit")
    if isinstance(submit, dict):
        candidates.append(submit.get("url"))
    for raw in candidates:
        if not raw:
            continue
        parsed = urllib.parse.urlsplit(str(raw))
        if parsed.scheme in ("http", "https") and parsed.netloc:
            # 生成接口多为 POST。退化检查只访问站点根地址，绝不碰生成路径。
            return {
                "url": urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/", "", "")),
                "method": "GET",
                "headers": {},
                "auth_checked": False,
            }
    return None


def test_connection(config: Any, entry_id: str, timeout_s: float = 12.0) -> dict[str, Any]:
    """安全地测试提供商网络和鉴权；只发 GET/HEAD，绝不提交生成任务。"""
    entry = get_entry(config, entry_id)
    if entry is None:
        raise ValueError("找不到这个提供商")

    api_key = get_key(config, entry_id) or ""
    if not api_key:
        return {"ok": False, "level": "error", "message": "未保存 API Key"}

    preset_cfg = dict((PRESETS.get(str(entry.get("preset") or ""), {})
                       .get("config") or {}))
    cfg = _deep_merge(preset_cfg, entry.get("config") or {})
    probe = cfg.get("connection_test")
    auth_checked = isinstance(probe, dict)
    if not auth_checked:
        probe = _safe_probe_from_config(cfg)
    if not isinstance(probe, dict) or not probe.get("url"):
        return {"ok": False, "level": "error", "message": "没有可测试的接口地址"}

    variables = {"api_key": api_key, "model": str(cfg.get("model") or "")}
    # 连通模板也能引用厂商声明的标量配置（例如业务空间 ID），而不是
    # 每增加一家就给这里补一个专用变量。
    variables.update({str(k): str(v) for k, v in cfg.items()
                      if isinstance(v, (str, int, float, bool))})
    probe = _test_template(probe, variables)
    url = str(probe.get("url") or "").strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"ok": False, "level": "error", "message": "接口地址格式不正确"}

    method = str(probe.get("method") or "GET").upper()
    if method not in ("GET", "HEAD"):
        return {"ok": False, "level": "error", "message": "连通检查只允许 GET 或 HEAD"}
    headers = {str(k): str(v) for k, v in (probe.get("headers") or {}).items()}
    headers.setdefault("Accept", "application/json")
    headers.setdefault("User-Agent", "AutoVid/connectivity-check")
    expected = {int(x) for x in (probe.get("success_statuses") or [200])}
    timeout_s = max(3.0, min(float(timeout_s), 30.0))
    started = time.monotonic()
    status = 0
    response_body = b""
    try:
        request = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            status = int(response.status)
            response_body = response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        response_body = exc.read(1024 * 1024)
    except TimeoutError:
        return {"ok": False, "level": "error", "message": f"连接超时（{timeout_s:g} 秒）"}
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        return {"ok": False, "level": "error", "message": f"接口不可达：{reason}"}
    except OSError as exc:
        return {"ok": False, "level": "error", "message": f"接口不可达：{exc}"}

    latency_ms = max(1, round((time.monotonic() - started) * 1000))
    common = {"status_code": status, "latency_ms": latency_ms}
    if status in expected:
        assertion = probe.get("json_assert")
        if isinstance(assertion, dict):
            try:
                parsed_body = json.loads(response_body.decode("utf-8", "replace") or "{}")
            except json.JSONDecodeError:
                return {"ok": False, "level": "error",
                        "message": "接口可达，但权限检查返回的不是有效数据", **common}
            actual = _test_dig(parsed_body, str(assertion.get("path") or ""))
            if "equals" in assertion and actual != assertion.get("equals"):
                message = str(probe.get("failure_message") or "接口可达，但模型权限未开通")
                return {"ok": False, "level": "error", "message": message, **common}
        message = str(probe.get("success_message") or
                      ("连接成功，Key 有效" if auth_checked else "服务地址可达，未验证 Key"))
        return {"ok": True, "level": "success", "message": message, **common}
    if status in (401, 403):
        return {"ok": False, "level": "error", "message": "认证失败，请检查 API Key", **common}
    if status == 402:
        return {"ok": False, "level": "error", "message": "接口可达，但账户余额不足", **common}
    if status == 429:
        return {"ok": True, "level": "warning", "message": "接口可达，但当前触发限流", **common}
    if not auth_checked and 200 <= status < 500:
        return {"ok": True, "level": "warning", "message": "服务地址可达，未验证 Key", **common}
    if status >= 500:
        return {"ok": False, "level": "error", "message": "接口可达，但服务端暂时异常", **common}
    return {"ok": False, "level": "error", "message": f"接口返回 HTTP {status}", **common}


# --------------------------------------------------------------------------- #
# 密钥
# --------------------------------------------------------------------------- #
def get_key(config: Any, entry_id: str) -> str | None:
    data = _read_json(_path(config, SECRETS_PATH), {})
    keys = data.get("provider_keys") or {}
    value = keys.get(entry_id)
    if value:
        return str(value)
    # 也支持直接用环境变量：AUTOVID_PROVIDER_<ID>
    import os  # noqa: PLC0415

    env_name = "AUTOVID_PROVIDER_" + re.sub(r"[^A-Za-z0-9]+", "_", entry_id).upper()
    return os.environ.get(env_name) or None


def set_key(config: Any, entry_id: str, api_key: str) -> None:
    path = _path(config, SECRETS_PATH)
    data = _read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    keys = data.setdefault("provider_keys", {})
    if api_key:
        keys[entry_id] = api_key
    else:
        keys.pop(entry_id, None)
    _write_json(path, data)


def preset_options() -> list[dict[str, Any]]:
    """给设置面板的下拉框用。

    带上 kind 和 capability：前端以前自己维护一份「哪些预设属于数字人」的
    硬编码名单，加一家就要改前端 —— 和 option_names 踩过的是同一个坑。
    现在用途由预设自己说了算，能力摘要也一起送出去，选的时候就能看见
    「这家出 1080×1920」还是「这家只出 512×512」。
    """
    from . import provider_caps as _caps  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for name, preset in PRESETS.items():
        if preset.get("hidden"):
            continue
        cfg = preset.get("config") or {}
        ui = PRESET_UI.get(name) or {}
        endpoint = ""
        if preset_kind(name) == "voice":
            endpoint = str(cfg.get("url") or "")
        else:
            submit = cfg.get("submit") or {}
            if isinstance(submit, dict):
                endpoint = str(submit.get("url") or "")
            endpoint = endpoint or str(cfg.get("submit_url") or "")
        models = copy.deepcopy(ui.get("models") or [])
        default_model = str(cfg.get("model") or "")
        if not models and default_model:
            models = [{"id": default_model, "label": default_model}]
        out.append({
            "name": name,
            "label": str(ui.get("label") or preset.get("label") or name),
            "homepage": preset.get("homepage", ""),
            "key_url": preset.get("key_url", ""),
            "kind": preset_kind(name),
            "endpoint": endpoint,
            "editable_label": bool(ui.get("editable_label")),
            "editable_endpoint": bool(ui.get("editable_endpoint")),
            "models": models,
            "default_model": default_model,
            "editable_model": bool(ui.get("editable_model")),
            "capability": _caps.capability_summary(cfg),
            "verified": bool((cfg.get("capabilities") or {}).get("verified")),
            "fields": copy.deepcopy(preset.get("fields") or []),
        })
    return out


# --------------------------------------------------------------------------- #
# 在编排层里的表示：voice:<id> / avatar:<id>
# --------------------------------------------------------------------------- #
def entry_name(entry: dict[str, Any]) -> str:
    """条目在 provider 体系里的名字，如 voice:voice-cosyvoice2。"""
    return f"{entry.get('kind')}:{entry.get('id')}"


def is_custom(provider: str) -> bool:
    return str(provider or "").startswith(("voice:", "avatar:"))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并：override 覆盖 base，但 base 里新增的键会保留。

    为什么需要：条目保存时会把预设**快照**下来。之后我给预设补了新字段
    （比如硅基流动的 max_audio_s=30），已有条目里没有这个字段，
    改动就永远传不进去 —— 实测表现为「修了上限问题但用户那边仍报同样错」。
    """
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve(config: Any, provider: str) -> dict[str, Any] | None:
    """把 `voice:<id>` / `avatar:<id>` 解析成调用所需的一切。

    返回 {kind, id, label, cfg, api_key, entry}；找不到返回 None。
    cfg = 预设模板（作为默认值）深合并条目的覆盖 —— 这样预设的后续改进
    能自动作用到已有条目上。
    """
    text = str(provider or "")
    kind, _, entry_id = text.partition(":")
    if kind not in KINDS or not entry_id:
        return None
    entry = get_entry(config, entry_id)
    if entry is None:
        return None
    preset_cfg = dict((PRESETS.get(str(entry.get("preset") or ""), {})
                       .get("config") or {}))
    cfg = _deep_merge(preset_cfg, entry.get("config") or {})
    return {
        "kind": kind,
        "id": entry_id,
        "label": str(entry.get("label") or entry_id),
        "cfg": cfg,
        "api_key": get_key(config, entry_id) or "",
        "entry": entry,
    }


def labels(config: Any) -> dict[str, str]:
    """{provider_name: 友好显示名}，给前端下拉框用。"""
    return {entry_name(e): str(e.get("label") or e.get("id"))
            for e in load_entries(config)}


def availability(config: Any, entry: dict[str, Any]) -> tuple[bool, str]:
    """条目现在能不能用，以及缺什么。"""
    preset_cfg = dict((PRESETS.get(str(entry.get("preset") or ""), {})
                       .get("config") or {}))
    cfg = _deep_merge(preset_cfg, entry.get("config") or {})
    kind = entry.get("kind")
    if kind == "voice":
        has_url = bool(cfg.get("url"))
        what = "base_url" if not has_url else "API Key"
    else:
        submit = cfg.get("submit")
        has_url = bool((isinstance(submit, dict) and submit.get("url"))
                       or cfg.get("submit_url") or cfg.get("base_url"))
        what = "提交地址" if not has_url else "API Key"
    has_key = bool(get_key(config, str(entry.get("id") or "")))
    required = (PRESETS.get(str(entry.get("preset") or ""), {}).get("fields") or [])
    missing_fields = [str(field.get("label") or field.get("name"))
                      for field in required
                      if field.get("required")
                      and not str(cfg.get(str(field.get("name") or "")) or "").strip()]
    if has_url and has_key and not missing_fields:
        return True, "已就绪"
    missing = []
    if not has_url:
        missing.append(what)
    if not has_key:
        missing.append("API Key")
    missing.extend(missing_fields)
    return False, "还缺 " + "、".join(missing)

"""推荐提供商清单 —— 设置在「推荐提供商」窗口里展示，带官网链接。

定位说明（诚实标注，不吹）：
    * `supports_clone`  是否能用你自己的音色（语音）/ 你自己的照片（视频）
    * `verified`        我们是否**真的实测过**这个接口。
                        只有 true 的才会写成可直接用的预设；false 的
                        只给链接，字段要自己对着文档填（设置面板支持改）。
                        本项目没买过谁的 Key，所以绝大多数是 False —— 这比
                        假装「填上就能用」然后报一堆错要好。

价格只写量级（"几分钱/千字"），因为各家随时改。点链接看官网最新报价。
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# 语音克隆 / TTS
# --------------------------------------------------------------------------- #
VOICE_PROVIDERS: list[dict] = [
    {
        "name": "硅基流动 SiliconFlow",
        "homepage": "https://cloud.siliconflow.cn/",
        "docs": "https://docs.siliconflow.com/cn/api-reference/audio/upload-voice",
        "tags": ["音色克隆", "国内直连", "最便宜"],
        "price": "CosyVoice2 约 ¥0.05/千字量级",
        "why": "上传一段参考音频就能克隆音色，价格是国内最低的一档，"
               "注册即用、无需企业认证。做短视频的量级非常划算。",
        "supports_clone": True,
        "verified": True,      # 接口形状已对着官方文档核对过（见下方 api_shape）
        "preset": "siliconflow",
        "api_shape": "POST /v1/uploads/audio/voice（multipart：model / customName / "
                     "text / 1.file）→ 拿 uri → POST /v1/audio/speech",
    },
    {
        "name": "MiniMax 开放平台",
        "homepage": "https://platform.minimaxi.com/",
        "docs": "https://platform.minimaxi.com/document/voice-cloning",
        "tags": ["音色克隆", "质量好", "国内"],
        "price": "按字符计费，中等偏上",
        "why": "海螺语音的音色克隆质量口碑好，情感和自然度高。"
               "两步流程（先上传音频拿 file_id，再建音色）。",
        "supports_clone": True,
        "verified": False,
        "preset": "minimax",
        "api_shape": "上传音频拿 file_id → 克隆建音色 → t2a_v2 合成",
    },
    {
        "name": "阿里云百炼 DashScope（CosyVoice 声音复刻）",
        "homepage": "https://bailian.console.aliyun.com/",
        "docs": "https://help.aliyun.com/zh/model-studio/cosyvoice-clone-api",
        "tags": ["音色克隆", "大厂", "稳定"],
        "price": "按量计费，需实名",
        "why": "阿里官方 CosyVoice 声音复刻，稳定性和合规性最好，"
               "适合要长期跑的账号。需要阿里云账号实名。",
        "supports_clone": True,
        "verified": False,
        "preset": "dashscope",
        "api_shape": "创建音色 → 用音色 ID 合成（HTTP，需 DashScope API Key）",
    },
    {
        "name": "火山引擎（豆包语音）",
        "homepage": "https://www.volcengine.com/product/voice-tech",
        "docs": "https://www.volcengine.com/docs/6561",
        "tags": ["音色克隆", "国内", "抖音同源"],
        "price": "按量计费",
        "why": "就是抖音背后的语音技术，中文口播的自然度很贴平台调性。"
               "需要开通「声音复刻」服务。",
        "supports_clone": True,
        "verified": False,
        "preset": "volcengine",
        "api_shape": "上传训练音频 → 训练音色 → 合成（火山签名鉴权）",
    },
    {
        "name": "ElevenLabs",
        "homepage": "https://elevenlabs.io/",
        "docs": "https://elevenlabs.io/docs/api-reference/voices/ivc/create",
        "tags": ["音色克隆", "最像真人", "英文强"],
        "price": "较贵（按字符）",
        "why": "英文克隆效果公认最好，中文也可以。缺点是贵，"
               "而且国内访问需要自己解决网络。",
        "supports_clone": True,
        "verified": False,
        "preset": "elevenlabs",
        "api_shape": "POST /v1/voices/add（multipart 上传样本）→ "
                     "POST /v1/text-to-speech/{voice_id}",
    },
    {
        "name": "Fish Audio",
        "homepage": "https://fish.audio/",
        "docs": "https://docs.fish.audio/api-reference/endpoint/openapi-v1/tts",
        "tags": ["音色克隆", "便宜", "多语言"],
        "price": "便宜",
        "why": "开源起家的多语言 TTS，克隆又快又便宜，中文表现不错。",
        "supports_clone": True,
        "verified": False,
        "preset": "fish_audio",
        "api_shape": "POST /v1/tts（带 reference_audio 或 model id）",
    },
    {
        "name": "任何 OpenAI 兼容接口",
        "homepage": "https://platform.openai.com/docs/guides/text-to-speech",
        "docs": "https://platform.openai.com/docs/api-reference/audio/createSpeech",
        "tags": ["通用", "好接"],
        "price": "看厂商",
        "why": "很多中转/自建服务都提供 /v1/audio/speech 兼容接口。"
               "如果你的服务是这个形状，直接选这个预设。",
        "supports_clone": False,
        "verified": False,
        "preset": "openai_compat",
        "api_shape": "POST /v1/audio/speech → 直接返回音频字节",
    },
    {
        "name": "自建 GPT-SoVITS / CosyVoice（本地或服务器）",
        "homepage": "https://github.com/RVC-Boss/GPT-SoVITS",
        "docs": "https://github.com/RVC-Boss/GPT-SoVITS/blob/main/docs/cn/README.md",
        "tags": ["音色克隆", "免费", "要自己部署"],
        "price": "免费（要显卡）",
        "why": "完全免费、音色最自由，但有部署门槛，且传统上需要 NVIDIA 显卡。"
               "你如果以后换了 N 卡，这条路最省钱。",
        "supports_clone": True,
        "verified": False,
        "preset": "http_json",
        "api_shape": "自建服务暴露一个 HTTP 接口（本项目已内置 http_json 适配）",
    },
]

# --------------------------------------------------------------------------- #
# 数字人 / 口播视频
# --------------------------------------------------------------------------- #
VIDEO_PROVIDERS: list[dict] = [
    {
        "name": "阿里云百炼 Wan2.2-S2V",
        "homepage": "https://bailian.console.aliyun.com/",
        "key_url": "https://bailian.console.aliyun.com/",
        "docs": "https://help.aliyun.com/zh/model-studio/wan-s2v-api",
        "tags": ["国内直连", "自然讲解", "新用户免费额度"],
        "price": "480P 每秒零点五元，720P 每秒零点九元，新用户赠送一百秒",
        "why": "人物照片和真实配音共同驱动口型、表情和肢体动作。国内访问方便，"
               "三十秒演示优先用四百八十清晰度，成本明显低于当前方案。",
        "supports_photo": True,
        "verified": False,
        "preset": "dashscope_s2v",
        "api_shape": "临时上传人物图和口播音频 → 提交异步任务 → 轮询并下载视频",
    },
    {
        "name": "fal.ai OmniHuman 1.5",
        "homepage": "https://fal.ai/models/fal-ai/bytedance/omnihuman/v1.5",
        "key_url": "https://fal.ai/dashboard/keys",
        "docs": "https://fal.ai/models/fal-ai/bytedance/omnihuman/v1.5/api",
        "tags": ["自然讲解", "表情动作", "音频驱动"],
        "price": "每秒约零点一六美元，当前账户已因余额耗尽被锁定",
        "why": "直接用人物照片和口播音频生成讲解视频，除了口型，还会生成自然眨眼、"
               "头部变化和克制的讲解手势，符合当前要跑通的真人口播方向。",
        "supports_photo": True,
        "verified": False,
        "preset": "fal",
        "api_shape": "提交人物图 + 口播音频 → fal.ai 队列 → 下载生成视频",
    },
    {
        "name": "Replicate",
        "homepage": "https://replicate.com/",
        "docs": "https://replicate.com/collections/lipsync",
        "tags": ["托管开源模型", "模型最全"],
        "price": "按秒计费",
        "why": "和 fal.ai 类似但模型库更大，wav2lip / sadtalker / latentsync / "
               "sync 之类的口型模型基本都有官方镜像。",
        "supports_photo": True,
        "verified": False,
        "preset": "replicate",
        "api_shape": "POST /v1/predictions → 轮询 status/output",
    },
    {
        "name": "MiniMax 海螺视频",
        "homepage": "https://platform.minimaxi.com/",
        "docs": "https://platform.minimaxi.com/document/video_generation",
        "tags": ["国内", "视频生成"],
        "price": "按量计费",
        "why": "国内可直连的视频生成，本项目已经内置了 minimax_h3 适配器。",
        "supports_photo": True,
        "verified": False,
        "preset": "minimax_h3",
        "api_shape": "本项目已内置适配器，只需填 Key",
    },
    {
        "name": "腾讯云数智人",
        "homepage": "https://cloud.tencent.com/product/ivh",
        "docs": "https://cloud.tencent.com/document/product/1240",
        "tags": ["国内", "企业级", "要认证"],
        "price": "企业级报价",
        "why": "国内合规性最好的数字人方案之一，适合商用账号。"
               "开通流程偏企业向。",
        "supports_photo": True,
        "verified": False,
        "preset": "custom_http_job",
        "api_shape": "提交任务 + 轮询（本项目 http_job 适配器可接）",
    },
    {
        "name": "阿里云智能媒体服务 IMS",
        "homepage": "https://www.aliyun.com/product/ims",
        "docs": "https://help.aliyun.com/zh/ims/",
        "tags": ["国内", "大厂"],
        "price": "按量计费",
        "why": "阿里云的数字人/智能媒体能力，和百炼语音可以配套使用。",
        "supports_photo": True,
        "verified": False,
        "preset": "custom_http_job",
        "api_shape": "提交任务 + 轮询",
    },
    {
        "name": "任何「提交 + 轮询」型服务",
        "homepage": "",
        "docs": "",
        "tags": ["通用"],
        "price": "看厂商",
        "why": "只要你的服务是「POST 提交任务拿 id，再轮询拿结果 URL」这个形状，"
               "本项目内置的 http_job 适配器就能接，不用改代码。",
        "supports_photo": True,
        "verified": True,      # 这套模式项目内已实测（smoke_cloud / smoke_comfy）
        "preset": "custom_http_job",
        "api_shape": "POST submit_url → 轮询 query_url → 下载视频",
    },
]


def catalog() -> dict[str, list[dict]]:
    """给前端用的推荐清单。"""
    return {"voice": VOICE_PROVIDERS, "video": VIDEO_PROVIDERS}

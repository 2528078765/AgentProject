# AutoVid —— 短视频口播数字人自动化流水线

把「选题 → 文案改写 → 音色克隆 → 出图 → 数字人 → 字幕 → 标题 → 一键发布」
串成一条可重跑、可局部重试的流水线。**带本地 Web 工作台，也支持纯命令行。**

当前默认路线是：**硅基流动 CosyVoice2 克隆音色 → 阿里云百炼 Wan2.2-S2V 根据配音
生成口型、表情、头部和手势动作 → FFmpeg 字幕与成片**。
这条路线不依赖 D-ID 或 HeyGen；适配器与本地模拟队列测试已经通过。
首次真实成片需先在「设置」里填写北京地域的百炼 API Key 和业务空间 ID。

语音和数字人都采用严格 API 模式：选中的 API 一旦失败，任务直接报错并停止，
不会自动换成默认音色、本地 Wav2Lip、静态画面或静音。

---

## 最快用法：Web 工作台

```powershell
cd path\to\shortvideo-agent
python -m autovid web --open
```

浏览器会自动打开 **http://127.0.0.1:8899/**（端口不可用时会自动换一个，并在终端显示实际地址）。

配置百炼 API Key 和业务空间 ID 后可直接运行：

```powershell
python -m autovid graph run --script-file "examples\自然口播样片.txt" `
  --scene-photo "scenes\demo-presenter-waist-up.png" --auto
```

场景图最好是正面、腰部以上、双手入镜；只有大头照时，即使模型能补身体，身份和手指稳定性也会明显下降。

页面上能做的事：

| 区域 | 功能 |
|---|---|
| 左栏 | 填选题、粘贴自有文案、按次切换「文案 / 语音 / 出图 / 数字人」provider |
| 右栏·进度 | 9 个节点实时亮灯（等待 / 运行中 / 成功 / 复用 / 失败）+ 实时日志滚动 |
| 右栏·结果 | 视频直接播放（可拖动进度条）、封面预览、标题候选点击复制、话题标签、发布简介 |
| 底部按钮 | 「下载发布包（zip）」一键拿到自包含上传目录；「打开运行目录」在资源管理器里定位 |
| 历史运行 | 点任意一条回看它的成片和文案 |

> 服务只监听 `127.0.0.1`，纯标准库 `http.server` 实现，不装任何依赖、不对外暴露。
> 想验证功能是否正常：`python scripts\smoke_web.py`（会真起一个服务并跑 26 项检查）。

---

## 音色库 / 形象库（上传你自己的音色和形象）

页面上有三个标签页：**创作 / 音色库 / 形象库**。

### 音色库

1. 输入名字 → 「新建」→ 页面展示**录音引导文案**（5 段，约 276 字，覆盖中文声韵调，写得像聊天而不是朗读稿，避免克隆出来一股播音腔）
2. 两种采集方式：
   - **浏览器直接录音** —— 点「开始录音」，读完点「停止并保存」（localhost 属于安全上下文，`getUserMedia` 可用，不需要 HTTPS）
   - **上传文件** —— 支持 wav/mp3/m4a/flac/ogg/opus/webm
3. 保存时自动**体检 + 规范化**：拒绝小于 3 秒或没有音轨的文件，统一转成 24kHz 单声道 PCM WAV
4. 保存后可试听、可删除

### 形象库

1. 输入名字 → 「新建」→ 按提示上传 **3~5 张**照片（正脸、微左、微右、带笑容）
2. 上传时体检：拒绝短边小于 256px 或无法解码的图片
3. 照片网格里可以**设为主图**、单张删除
4. 主图就是数字人驱动用的那一张

### 资产 ID 进配置，因此自动参与缓存失效

选中的音色/形象会写进 `project.voice_id` / `project.avatar_id`，而这两个字段在
`voice` 和 `avatar` 步骤的 `config_keys` 里 —— 所以**换音色或换形象会自动让下游步骤失效重跑**，
不需要手动清缓存。这正是"资产要放进 State"的实际含义。

```
assets/voices/<voice_id>/     meta.json + ref.wav + model/
assets/avatars/<avatar_id>/   meta.json + photos/ + model/
```

`assets/` 已在 `.gitignore` 里 —— 音色和照片是你的个人生物特征数据，**永远不要提交到 git**。

> 当前默认数字人引擎是 `avatar:avatar-dashscope-wan-s2v`：人物动作和口型都由配音直接驱动。
> `still` 仍保留为手动选择的离线测试方案，但严格模式不会自动切过去。

---

## 命令行用法（等价能力）

```powershell
cd path\to\shortvideo-agent

# 环境自检（会真的去合成一次语音，而不是只看列表）
python scripts\check_env.py

# 跑一条完整的视频
python -m autovid run --topic "为什么你越努力越焦虑"

# 看这次运行产出了什么
python -m autovid show
```

产出在 `runs\<时间>-<选题>\`：

```
runs/20260911-125814-为什么你越努力越焦虑/
  manifest.json          # 运行清单：每一步的状态、耗时、产物 sha256
  publish/               # ★ 自包含发布包，直接拿去上传
    video.mp4            #   成片 1080x1920
    cover.jpg            #   竖版封面
    cover_3x4.jpg        #   3:4 封面
    发布文案.txt          #   3 个标题候选 + 简介 + 话题标签
    发布清单.md           #   逐项勾选的发布检查表（含合规项）
  work/                  # 中间产物（每一步一个子目录）
  _logs/<run>/           # 每一步的完整日志
```

---

## 四个让它不沦为玩具的设计

### 1. Artifact 契约 + input_hash —— 局部重跑的地基

每一步都声明 `requires` / `produces`，产物落盘时带 sha256。步骤的输入指纹是：

```
input_hash = hash(步骤版本 + 相关配置 + 上游产物哈希 + CLI 输入)
```

于是：**输入没变就跳过**（不重复烧 GPU／不重复花钱），**改了一个环节只重跑受影响的步骤**。

```powershell
# 只看计划不执行
python -m autovid run --topic "..." --dry-run

# 字幕调了字号？只重跑字幕 + 合成 + 发布，前面全部复用
python -m autovid rerun --only subtitles,compose,publish

# 换了选题 → 自动从 script 开始重跑
python -m autovid run --topic "换一个选题"
```

实测：第一次运行 24.5s，中途失败 4 次，每次都只重跑了失败的步骤，
前面的 `topic/script/voice/visuals/avatar/subtitles` 全部复用磁盘产物。

### 2. 字幕用 TTS 的逐句时间戳，不用 ASR 反推

语音是**逐句合成**的，再直接拼 PCM 样本。每句的起止时间由样本数精确算出，
字幕时间轴天然与口型对齐，比「先生成视频再跑 ASR 对齐」准一个量级，还省一次模型推理。

### 3. 数字人只产出「镜头片段」，合成逻辑与模型解耦

`avatar` 这一步的输出契约是「每段一个视频片段」。
现在挂的是离线实现（静图 + 推镜），换成 HeyGem / MuseTalk / 云端 API 时，
**下游 `subtitles` / `compose` / `publish` 一行都不用改**。

### 4. 所有文字走 ASS 层，不用 drawtext

libass 天生处理中文、字体、换行、描边；`drawtext` 在 Windows 上要跟
`C\:/path/...` 转义搏斗。字幕、顶部关键词条、封面标题全部复用同一套 ASS 机制
（见 `autovid/media.py`）。

---

## 流水线：9 个步骤

| # | 步骤 | 做什么 | 产物 |
|---|---|---|---|
| 1 | `topic` | 选题（CLI 指定 / 内置选题池兜底） | `topic.json` |
| 2 | `script` | 改写成结构化口播稿（分段 + 上屏关键词 + 出图提示词） | `script.json` |
| 3 | `voice` | 音色克隆 / TTS，逐句合成 + 精确时间轴 | `voice.wav`、`voice_segments.json` |
| 4 | `visuals` | 背景图 / 封面底图 | `visuals.json` + 每段一张图 |
| 5 | `avatar` | 数字人驱动（★ 换真实模型就在这一步） | `avatar.json` + 每段片段 |
| 6 | `subtitles` | 生成 ASS（正文字幕 + 顶部关键词条） | `subtitles.ass` |
| 7 | `compose` | 拼视频轨 → 接音频 → 烧字幕 → 渲染两种封面 | `final.mp4`、`cover*.jpg` |
| 8 | `metadata` | 标题候选 / 简介 / 话题标签 | `metadata.json` |
| 9 | `publish` | 生成自包含发布包 | `publish/` |

常用命令：

```powershell
python -m autovid providers              # 看各 provider 可用性
python -m autovid run --script-file 文案.txt   # 用你写好的文案（自动切段）
python -m autovid runs                   # 列出所有运行
python -m autovid show --run latest      # 查看某次运行的清单
python -m autovid step voice --run latest        # 只跑一步（调试用）
python -m autovid step voice --run latest --force # 强制重跑一步
python -m autovid purge --keep 5         # 清理旧运行（视频很占地方）
```

---

## 怎么换成真能力

改 `config/pipeline.json` 里的 `provider` 字段即可。每个环节都是可插拔的。

### 音色克隆（`steps.voice.provider`）

| 值 | 说明 | 状态 |
|---|---|---|
| `voice:voice-cosyvoice2` | **当前默认：硅基流动 CosyVoice2，使用音色库中已克隆的声音** | 需要 Key 与有效克隆音色 ID |
| `edge_native` | 零依赖的 Edge 在线 TTS（自己实现的 WebSocket 客户端），免费、无需 Key、中文自然 | 手动测试；不是你的声音 |
| `cloud_tts` | **厂商无关的云端 TTS / 音色克隆适配器** —— 唯一能真正克隆你音色的现实路径 | 需要 Key，见下节 |
| `sapi` | Windows 内置，离线免费 | 受限/非交互会话下会被系统拒绝 |
| `edge` | pip 版 edge-tts | 需要 `pip install edge-tts` |
| `http_json` | 自建 GPT-SoVITS / CosyVoice | 需要 NVIDIA GPU |
| `silent` | 等长静音，永远可用 | 仅供测试；严格模式不会自动使用 |

---

## 怎么真正克隆你自己的音色

**这台机器的硬约束**（实测，不是猜的）：

```
torch 2.13.0+cpu     ← CPU-only 构建，插了 NVIDIA 卡也用不了
CUDA 可用: False      6 个 CPU 线程
缺少 torchaudio / librosa / soundfile / onnxruntime
```

GPT-SoVITS、CosyVoice、XTTS 这些克隆模型**连 import 都做不到**，而 pip 在这台机器上装不了包。
所以本地克隆走不通；数字人「嘴动」在 CPU 上一条 50 秒视频要按小时算，同样走不通。

**云端是唯一现实路径。** 为此我写了 `cloud_tts` —— 一个**厂商无关**的适配器：
请求体、请求头、响应字段路径全部用模板描述在 `config/pipeline.json` 里，
换厂商只改 JSON，不用改一行代码。

### 三步接上

**第 1 步：挑一家厂商注册，拿 Key**

| 厂商 | 克隆 | 备注 |
|---|---|---|
| MiniMax 海螺语音 | ✅ | 两步流程（上传文件 → 建音色），已在预设里实现 |
| 硅基流动 SiliconFlow | ✅ | CosyVoice2，表单上传换 voice URI，已在预设里实现 |
| Fish Audio | ✅ | 零样本克隆 |
| 阿里云百炼 CosyVoice | ✅ | 有免费额度 |
| 火山引擎声音复刻 | ✅ | 需企业资质 |
| OpenAI /v1/audio/speech | ❌ | 只能选内置音色 |

**第 2 步：探测接口字段**

```powershell
# 看有哪些厂商预设
python scripts\probe_cloud.py --list

# 用你自己录的音色跑一遍「克隆 + 合成」全流程
python scripts\probe_cloud.py --preset minimax --key 你的Key --asset voice-your-id
```

它做三件事：**打印服务端原始响应**（字段名以实际为准，文档经常对不上）、
**实测音频电平**（静音轨也算"合成成功"，必须量）、
**指出是否真的克隆了你的音色**。

探测通过后加 `--write-config` 就把模板写进配置了。

**第 3 步：放 Key 并切换**

```json
// config/secrets.json（已在 .gitignore 里）
{ "AUTOVID_CLOUD_TTS_KEY": "你的Key" }
```

然后在页面上把「语音引擎」切到 `cloud_tts`，音色选你自己的。

### 关于克隆注册：只跑一次

克隆是**一次性动作**，云端返回的音色 ID 会缓存进资产
（`assets/voices/<id>/meta.json` 的 `cloud_voice_ids`）。
**绝不会每次生成视频都重新克隆一遍** —— 那既慢又费钱。
测试里专门验证了这一点：第二次调用 `clone` 计数仍然是 1。

换参考音频后想重新克隆，清掉缓存即可（`AssetStore.clear_cloud_voice_ids`）。

### 适配器支持的能力

- 音频返回形式：`hex` / `base64` / `url` / 原始字节流（四种都有测试覆盖）
- 响应字段路径：点号表示，支持数组下标，如 `data.audios.0.url`
- 克隆流程：一步式，或**两步式**（先上传文件换 file_id，再建音色）
- 上传方式：JSON base64，或**表单 multipart**

`scripts/smoke_cloud.py` 用本地 mock 服务把上面这些全部验证过，
所以拿到 Key 后**只剩"字段名对不对"这一件事**需要核对。

当前工作流使用严格模式：`strict: true` 且 `fallback: []`。语音 API 返回空音频、超时或拒绝时，
流程会在语音步骤直接失败并点明接口错误，不会再换成系统默认音色、静音或本地引擎。

**怎么知道到底有没有真的用上你的音色？** 每次运行都会在 `voice_segments.json` 里记录
`cloned` / `voice_id` / `voice_name`，页面上也会直接显示：

- 绿色条：`已用你的音色「XXX」`
- 红色错误：所选克隆接口失败的具体原因

命令行体检：`python scripts\smoke_tts.py` —— 它会逐个 provider 实际合成并**用 ffmpeg 量出电平**，
区分「真出声」和「安静的假成功」（静音轨也算合成成功，光看日志会被骗）。

```json
"voice": {
  "provider": "voice:voice-cosyvoice2",
  "fallback": [],
  "strict": true,
  "gap_ms": 220
}
```

> **关于"你的音色"**：`edge_native` 用的是微软的晓晓，**不是你的声音**。
> 上传录音只是准备好素材，真正克隆需要一个克隆引擎。你这台机器是
> AMD RX 6750 GRE（RDNA2）+ Windows，**没有可用的 ROCm，跑不了 GPT-SoVITS / CosyVoice**，
> 所以现实路径是**云声音克隆 API**（MiniMax Speech、火山、阿里 CosyVoice 云服务等，
> 上传 10 秒录音即可零样本克隆），或租一台 NVIDIA 云 GPU 自建。

### 出图（`steps.visuals.provider`）

| 值 | 说明 |
|---|---|
| `ffmpeg_gradient` | 离线渐变背景，零依赖。整条视频统一配色 |
| `openai_images` | 任何 OpenAI 兼容的 `images/generations` 端点（SiliconFlow、通义万相等） |
| `comfy` | ComfyUI `/prompt` + `/history` 轮询；需要一份 API 格式工作流，用 `{{PROMPT}}/{{WIDTH}}/{{HEIGHT}}/{{SEED}}` 占位 |

### 数字人（`steps.avatar.provider`）★ 核心环节

| 值 | 说明 |
|---|---|
| `still` | 离线：把你的照片合成到背景上（图文口播，**不会动嘴**） |
| **`avatar:avatar-dashscope-wan-s2v`** | **当前默认：百炼 Wan2.2-S2V 用场景照片和配音生成口型、表情与动作；长口播自动拆分后合并** |
| `avatar:avatar-fal-ai-omnihuman-1-5` | 已停用：质量方向合适，但本账户余额耗尽且单价较高 |
| `avatar:avatar-siliconflow-motion` | 已停用：普通图生视频只有约 5 秒动作且不理解配音，长口播会重复、突变 |
| `local_wav2lip` | 本地照片口型驱动；只解决嘴部同步，不生成手势 |
| `comfy` | ComfyUI 数字人工作流；适合后续换成本地人物动画模型 |
| `http_job` | 通用「提交任务 + 轮询结果」型云服务（硅基智能 / 智影 / HeyGen / D-ID） |
| `minimax_h3` | MiniMax H3 / 海螺 云端 API |

`avatar:avatar-dashscope-wan-s2v` 使用 `config/providers.json` 里的请求模板；API Key 单独保存在
已忽略的 `config/secrets.json`，不会写入可提交配置。没有 Key 或业务空间 ID 时，前置检查会直接提示并阻止开跑。

---

## AMD 显卡怎么办：ComfyUI + comfyui-rocm

**先说一个我之前说错、必须纠正的结论。**

我早前说过「Windows 上没有可用的 ROCm，AMD 卡跑不了本地模型」。
那个判断基于我旧的认知，**现在已经过时**。新情况是：

> **comfyui-rocm**（[patientx-cfz/comfyui-rocm](https://github.com/patientx-cfz/comfyui-rocm)）
> 使用 **AMD 官方 ROCm 和 PyTorch**（TheRock 仓库），
> 支持 **GCN5/Vega、RDNA1、RDNA2、RDNA3、RDNA4** 全部 GPU，
> 自动安装 **Triton、Sage Attention、Flash Attention、bitsandbytes**，
> 并明确写着 *"Flash Attention is now available for all RDNA GPUs"*。

你的 **RX 6750 GRE 是 RDNA2（gfx1031），在支持列表里**。
这意味着它不是只能跑 ZLUDA 转译层，而是能跑真正的 ROCm PyTorch ——
**ComfyUI 数字人工作流从「基本没戏」变成了「值得一试」。**

（另一条对照：**HeyGem 仍然不行**。它官方文档写死了
*"The three services won't start without an NVIDIA graphics card"*，
整条链路绑定 CUDA，跟显存多大无关。）

### 安装要点

1. 装 `comfyui-rocm` —— **不要装进用户目录或 Program Files，路径不能有中文**，
   建议直接装在盘根目录，例如 `D:\comfyui-rocm`
2. 在里面装数字人节点：`ComfyUI_wav2lip` / MuseTalk / Sonic 等
3. 在 ComfyUI 里搭好工作流：**形象图 + 音频 → 视频**
4. 用「**导出 (API)**」保存成 JSON
5. 把输入图片改成 `"{{IMAGE}}"`、输入音频改成 `"{{AUDIO}}"`，
   数字字段写成 `"width": {{WIDTH}}`（**占位符在引号外**）
6. 放进 `config/workflows/avatar.json`，并设：

```json
"providers": {
  "comfy": {
    "url": "http://127.0.0.1:8188",
    "avatar_workflow": "config/workflows/avatar.json",
    "avatar_timeout_s": 1800
  }
}
```

页面上把「数字人引擎」切到 `comfy` 即可。

> **可用占位符**：`{{IMAGE}}` `{{AUDIO}}` `{{WIDTH}}` `{{HEIGHT}}` `{{FPS}}`
> `{{DURATION}}` `{{SEED}}`。
> 数字字段即使被误写进引号，系统也会自动转回数字 —— 这个坑不用你踩。
> **换一个数字人工作流不用改任何代码**，只换 JSON。

### 显存现实（你是 10GB）

| 模型 | 显存 | 在你卡上 |
|---|---|---|
| Wav2Lip | 4–6 GB | ✅ 可以 |
| MuseTalk | 8–12 GB | ⚠️ 勉强，需降分辨率 / 批量 |
| Sonic | 10 GB+ | ⚠️ 很紧 |
| Hallo2 / LatentSync | 16–24 GB | ❌ 不行 |

另：**Wav2Lip 的许可是非商用**，别拿它做变现内容。

### 编排层已经就绪

`autovid/comfy.py` 是一个完整的 ComfyUI 客户端（上传 / 提交 / 轮询 / 取产物），
`scripts/smoke_comfy.py` 用**本地 mock ComfyUI** 把整条链路验证过：

```
[通过] IMAGE / AUDIO / 数字字段 注入正确
[通过] 形象图只上传一次（不重复传）
[通过] 每段音频都上传了
[通过] 每段产出一个片段，且能被后续合成使用
[通过] 没配工作流时指出怎么配 / 文件不存在时给出路径 / 缺形象时指出原因
[通过] 无输出时提示检查输出节点（而不是傻等到超时）
[通过] ComfyUI 没启动时提示检查服务
```

所以你的 ComfyUI 一起来，接上就能用，不需要改代码。

## LangGraph 编排（自包含实现）

之前我两次以「装不了 langgraph」为由把这件事推后，那是我的问题 ——
**网络是通的，wheel 本质就是 zip**，完全可以绕开 pip 手工 vendor。
现在 LangGraph 真实可用，而且**编排是重写的**：节点里放真实逻辑，
不经过 `pipeline.Runner`、不经过 `steps.py`、也不依赖 manifest。

### 装上它（绕开 pip）

```powershell
python scripts\vendor_deps.py langgraph
python scripts\vendor_deps.py langgraph-checkpoint-sqlite
```

`scripts/vendor_deps.py` 是一个手工版 pip：从 PyPI 递归解析依赖树、
**遵守版本约束**、挑选匹配当前解释器的 wheel（会校验 cp3XX **和 abi 标签**，
避免把自由线程版 `cp314t` 装进来）、下载并解包到 `.pylibs/`。
`autovid/__init__.py` 会自动把 `.pylibs` 挂到 `sys.path`。

实测：langgraph 1.2.11 + 39 个依赖（含 `orjson` / `pydantic-core` / `pyyaml`
等 CPython 3.14 二进制包），**22.8 MB，全部搞定**。

### 用起来

```powershell
# 一路跑完（默认不开闸门）
python -m autovid graph run --topic "选题"

# 打开人工闸门
python -m autovid graph run --topic "选题" --gates script,voice,publish
python -m autovid graph run --topic "选题" --interactive   # 终端里审批

# 从闸门处恢复（可以换进程、换一天）
python -m autovid graph resume --thread <id> --decision approve
python -m autovid graph resume --thread <id> --decision reject
python -m autovid graph state  --thread <id>
```

缺少数字人 Key 时会在花费额度前直接停止：

```
检查点  ：sqlite（sqlite 可跨进程恢复）
人工闸门：全部关闭
【前置判断】发现 1 个问题
    ✓ 文案来源：选题「LangGraph自包含编排」
    ✓ 音色：csy（67.02s 参考音频）
    ✓ 形象：csy（3 张照片）
    ✗ 数字人接口「avatar:avatar-dashscope-wan-s2v」不可用：还缺 API Key、业务空间 ID
生成前的检查没通过，已中止
```

### 图的形状

```
START
  │
  ▼
preflight ──(缺东西)──> fail ──> END
  │
  └──(齐全)──> voice_clone ──> script ──> tts ──> avatar
                                                    │
        END <── publish <── metadata <── compose <── subtitles
```

**没有「背景图」节点。** 画面与背景来自**本次拍摄的场景照片**：

| | 存什么 | 频率 |
|---|---|---|
| **形象库** | 你是谁（身份特征） | **采集一次，长期复用** |
| **本次场景照片** | 今天在哪拍（带人物的照片） | **每次生成现拍一张** |

所以背景天然每次都不同 —— 在客厅拍就是客厅，在户外拍就是户外，
不需要再花钱生成背景图。

节点与职责：

| 节点 | 干什么 |
|---|---|
| **preflight** | 判断必备条件：**文案**（选题/文案内容/文案文件三者有其一）、**音色**（存在且参考音频可读）、**形象**（存在且有照片）、**场景照片**（存在、可解码、最短边 ≥256px）、输出目录可写。引擎能力问题只警告不阻断 |
| **fail** | 说明错误原因 + 给出修复指引，然后走向 END |
| **voice_clone** | 把参考音频变成可用音色：云端则注册克隆（结果缓存进资产，不重复注册），本地则校验 |
| **script** | 有自有文案就用它切段，否则让 LLM/模板生成结构化口播稿 |
| **tts** | 按标点切「气口句」→ 逐句合成 → 按分级停顿拼接 → 得到精确时间轴 |
| **avatar** | 用**场景照片**当画面 + 语音驱动 → 会动的口播片段。`local_wav2lip`（默认）**嘴会跟着声音动**；`still` 只做缓慢推镜。形象照作为身份参考传给 ComfyUI 工作流的 `{{IDENTITY_IMAGE}}` |
| **subtitles** | ASS 字幕：一条正文 = 一口气，时间戳来自 PCM 样本数 |
| **compose** | 拼视频轨 → 接音频 → 烧字幕 → 渲染两种封面 |
| **metadata** | 标题候选 / 简介 / 话题标签 |
| **publish** | 自包含发布包（成片 + 封面 + 文案 + 清单） |

> **为什么默认不把形象照叠到画面上**：场景照片里已经有人了，再叠一次
> 画面里会出现两个人。只有当场景照片是纯环境照（没人）时，才把
> `avatar.use_identity_overlay` 设为 true。

### 两个本地引擎（默认）

声音和嘴型都由**本机**完成 —— 不花钱、不联网、数据不出门：

| 环节 | 引擎 | 部署命令 | 实测（RX 6750 GRE 混合 CPU） |
|---|---|---|---|
| **语音合成** | Qwen3-TTS-0.6B（Talker/Predictor 走 llama.cpp **Vulkan**，Decoder 走 ONNX） | `python scripts/deploy_qwen3tts.py` | 引擎加载 2.7s；克隆首次 43s、之后 **0s**；稳态 **RTF 0.72** |
| **数字人** | Wav2Lip（36M 参数，CPU） | `python scripts/deploy_wav2lip.py` | **RTF ≈ 0.94**（比实时快） |

三条关键设计：

* **克隆一次，反复用**：参考音频提取为无损锚点
  `assets/voices/<id>/anchor.json`（音频码 + 1024 维说话人嵌入），
  之后每次合成直接载入，不重算。
* **进程隔离**：llama.cpp 与 torch 各带一个 OpenMP 运行时
  （`libomp.dll` / `libiomp5md.dll`），同进程会 `OMP Error #15` 直接崩。
  所以 Wav2Lip 跑在独立子进程里，一次进程渲完全部片段。
* **音频处理自实现**：原版 Wav2Lip 依赖 librosa，而 librosa 依赖 numba
  （Python 3.14 没有轮子）。项目里精确复刻了它的 hparams ——
  **Slaney mel 刻度**（不是 HTK）、preemphasis、librosa 风格
  `center+reflect` STFT、对称归一化。这四处错一个，口型就会错位。

端到端实测：**167 秒产出一条 53.4 秒视频**（你自己的声音、会动的嘴、
字幕、封面、发布包）。验证脚本 `scripts/smoke_e2e_local.py`。

前置条件不满足时的真实输出：

```
生成前的检查没通过，已中止：
  ✗ 文案没准备好：既没有给选题，也没有给文案内容或文案文件
  ✗ 音色没准备好：没有选择要克隆的音色
  ✗ 形象没准备好：没有选择形象参考图

怎么修：打开页面 http://127.0.0.1:8899/ ，在「音色库 / 形象库」里补上缺的东西并选中；
        文案来源在「创作」页填选题，或粘贴自有文案。
```

### 自包含：不调用之前的编排代码

`scripts/smoke_flow.py` 用**三种方式**证明这一点（不是靠我口头保证）：

```
[通过] 没有 import pipeline / steps / manifest / cli / web
[通过] 代码里没有出现 Runner / RunContext / StepDef     ← AST 解析标识符
[通过] 导入 graph.py 不会连带加载 pipeline / steps / manifest  ← sys.modules 动态检查
[通过] 复用的是能力层（providers/media/assets）
```

第一、二项用 `ast` 解析模块，看的是真实的 import 与标识符，
而不是字符串匹配（文档字符串里提到 "Runner" 是说明文字，不算调用）。

复用的是**能力层**（抽象干净，没必要重写）：

| 模块 | 复用理由 |
|---|---|
| `providers` | TTS / 出图 / 数字人 / 发布 / LLM 改写，本来就是可插拔的能力抽象 |
| `media` | ffmpeg 封装、ASS 字幕、断句与气口、媒体体检 |
| `assets` | 音色与形象资产库 |
| `comfy` | ComfyUI 客户端 |

**不复用**：`pipeline.py`（自研 Runner）、`steps.py`、`manifest.py`（Artifact 缓存）。

代价要说清楚：图这条路**没有 artifact 级缓存**，靠的是 LangGraph 的 checkpointer ——
跑到一半中断，恢复时已完成的节点不会重跑。两者是不同的取舍：
`autovid run` 适合反复微调同一批素材，`autovid graph run` 适合带人审的长流程。

### 三个真 bug（都是这轮测试抓出来并修掉的）

1. **恢复时传错了载荷** —— 我把恢复值直接当输入传给 `invoke`，
   但 LangGraph 要求 `Command(resume=值)`。后果是闸门拿不到决定值、原地打转。
2. **`_context()` 每次重新打开 RunContext** —— 同一次图执行里会有多个实例，
   各自的 manifest 在内存里互相覆盖，**会丢产物**。现在会缓存复用。
3. **vendor 脚本忽略版本约束** —— 无脑取最新版，装出 `pydantic-core 2.49.0`
   而 `pydantic 2.13.5` 要求 `==2.46.5`，直接 `SystemError`。
   现在会把所有约束合并求解，并**校验 abi 标签**（`cp314t` 自由线程版不能装在 `cp314` 上）。

### 文案改写（`steps.script.provider`）

| 值 | 说明 |
|---|---|
| `offline` | 不联网。有 `--script-file` 就切段，否则生成**结构正确的占位稿** |
| `openai_compat` | 任何 OpenAI 兼容端点（DeepSeek / 通义 / 智谱 / 本地 vLLM） |

配好 Key 后把 `offline` 改成 `openai_compat` 即可。密钥放 `config/secrets.json`
或环境变量（`AUTOVID_LLM_API_KEY`），**不要写进 `pipeline.json`**。

```json
// config/secrets.json （已被 .gitignore 忽略）
{ "AUTOVID_LLM_API_KEY": "sk-..." }
```

内置的抖音口播稿提示词在 `autovid/providers.py` 的 `SCRIPT_SYSTEM_PROMPT`，
已经约束了：前 3 秒钩子、口语化、25 字以内短句、禁止编造数据和绝对化承诺。

---

## 关于你的硬件和 MiniMax H3

> ⚠️ **本节已更新**。下面这段最早的判断（「Windows 上没有可用的 ROCm」）
> 已经过时，请以「AMD 显卡怎么办」那一节为准。

**RX 6750 GRE 是 RDNA2（gfx1031）**。当时我判断 Windows 上跑不了 ROCm，
所以本地模型这条路走不通。**这个结论现在不成立了** ——
`comfyui-rocm` 已经能用 AMD 官方 ROCm + PyTorch 支持 RDNA2，
详见「AMD 显卡怎么办」一节。

仍然成立的几条：

- **HeyGem 依然不行**：它官方要求 NVIDIA，整条链路绑定 CUDA，与显存无关。
- **MiniMax H3 权重依然跑不动**：社区量化版是 `nvfp4 / INT4 / INT8 / DT-sQKV`，
  其中 `nvfp4` 是 **NVIDIA Blackwell 专属格式**，工具链全是 CUDA 向的。
  （它的云端 API 不受影响。）
- **10GB 显存是硬约束**：MuseTalk 勉强、Hallo2 / LatentSync 不行。

现实的组合方案：

| 环节 | 建议 |
|---|---|
| 编排 / 字幕 / 合成 / 发布包 | **本地**（就是本项目，零 GPU 依赖） |
| 文案改写 | 云端 LLM API（DeepSeek 等，几分钱一条） |
| 音色克隆 | **本地 Qwen3-TTS**（Vulkan/DirectML/CPU 都行）或云端 TTS |
| 出图 / 封面 | 本地 ComfyUI（ROCm）或云端 API |
| 数字人 | **本地 ComfyUI 工作流（ROCm）** 或云端 API |

## 合规红线（务必看）

1. **AI 内容标识**：《人工智能生成合成内容标识办法》2025-09-01 起施行，
   AI 生成的视频必须**显式 + 隐式标识**。发布清单里专门留了这项，别跳过。
2. **声音权**：克隆音色必须获得本人授权。《民法典》保护声音权益，
   克隆他人（尤其是名人）声音已有判例认定为侵权。
3. **著作权**：`script` 步骤的提示词强制「用自己的语言重构、不得逐句照搬」，
   但**你自己要复核**。洗稿搬运是有法律风险的。
4. **自动发布的风险**：抖音对自动化发布风控严格，Playwright 脚本方案存在**封号风险**。
   本项目的立场是：**优先申请抖音开放平台走官方接口；在拿到资质前，
   用「一键备好 + 人工点发布」**。所以 P0 只生成发布包，不碰账号自动化。

---

## 项目结构

```
autovid/
  config.py      配置加载（内置默认值 <- pipeline.json <- secrets.json <- 环境变量）
  errors.py      统一异常（库代码绝不抛 SystemExit，见文件内说明）
  manifest.py    Artifact 契约 / RunContext / input_hash / 缓存与局部重跑
  media.py       FFmpeg 封装、ASS 字幕、媒体体检、人物合成、离线素材生成
  assets.py      音色与形象资产库：采集规范、上传体检、目录结构
  edge_tts_native.py  零依赖 Edge TTS（自实现 WebSocket + Sec-MS-GEC 鉴权）
  providers.py   可插拔 Provider：LLM / TTS / 出图 / 数字人 / 发布
  steps.py       9 个步骤的具体实现
  pipeline.py    引擎：依赖解析、计划、缓存跳过、失效传播、进度事件
  cli.py         命令行
  web/
    server.py    零依赖 HTTP 服务：SSE 进度、Range 视频、发布包 zip、资产接口
    static/index.html   单文件前端（无 CDN、无外部依赖）
config/
  pipeline.json  主配置（改这里切换 provider）
scripts/
  check_env.py   环境自检
  smoke_media.py 媒体层冒烟测试（不经过流水线）
  smoke_tts.py   语音体检：逐个 provider 实测电平
  smoke_web.py   Web 工作台冒烟测试（起真实服务跑 26 项检查）
  smoke_assets.py 资产层冒烟测试（采集体检 / 缓存失效 / 人物合成）
examples/
  文案示例.txt    配合 --script-file 使用
```

---

## HTTP 接口（想自己接前端 / 接若依 / 接 n8n 就看这里）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 工作台页面 |
| GET | `/api/bootstrap` | 步骤定义、可选 provider、能力探测、历史运行 |
| POST | `/api/plan` | 只出运行计划，**不落盘、不执行** |
| POST | `/api/run` | 发起一次运行，返回 `run_id` |
| GET | `/api/stream?run_id=` | SSE 进度流（`plan` / `step_start` / `step_log` / `step_done` / `run_done`） |
| GET | `/api/result?run_id=` | 结果负载（成片、封面、标题、标签、时长） |
| GET | `/api/runs` | 历史运行列表 |
| GET | `/api/logs?run_id=&step=` | 某一步的完整日志 |
| GET | `/media?run_id=&p=` | 读取产物，**支持 Range**（视频拖动播放依赖它） |
| GET | `/package?run_id=` | 下载自包含发布包 zip |
| POST | `/api/reveal` | 在资源管理器里打开该运行目录 |

`POST /api/run` 的请求体：

```json
{
  "topic": "为什么你越努力越焦虑",
  "script_text": "（可选）你自己写好的文案，填了就不做 AI 改写",
  "overrides": {
    "steps.voice.provider": "edge",
    "steps.avatar.provider": "still"
  },
  "force": false
}
```

> `overrides` 会被写进该次运行的 manifest 并参与 `input_hash`，
> 所以「同一个选题换了语音 provider」会被正确识别为输入已变、自动重跑对应环节。

---

## 关于用 LangGraph 重写编排

**可以，而且这个引擎本来就是按「节点 + 依赖 + 状态」设计的**，换成 LangGraph
主要是换一层编排外壳，业务逻辑一行都不用动：

| 现有概念 | LangGraph 对应 |
|---|---|
| `StepDef.requires` / `produces` | `StateGraph` 的 `add_edge` |
| `RunContext.load_artifact()` | 节点函数从 state 取上游数据 |
| `input_hash` 缓存跳过 | 条件边（命中缓存就跳过该节点） |
| `Runner._emit()` 事件流 | `astream()` / `StreamWriter` |
| `manifest.json` | Checkpointer（`SqliteSaver` 等） |

**但有两个前提要想清楚**：

1. **别把 GPU/IO 任务也做成 Agent 节点**。这条链路里只有 4 个地方需要 LLM 决策
   （选题、改写、出图提示词、标题标签），其余全是确定性的 ffmpeg / 模型调用。
   把它们包成「Agent 节点」只会让调试变难、成本失控。LangGraph 的价值在于
   **条件分支 + 人工审核中断 + 持久化 checkpointer**，不是在于「用了 Agent」。
2. **LangGraph 依赖放在项目的 `.pylibs` 中**，不污染系统 Python；当前图编排、SQLite
   checkpointer、人工闸门和跨进程恢复都已通过冒烟测试。

真正值得用 LangGraph 加的两个能力：`interrupt()` 做「文案确认 / 试听音色 / 发布确认」
三道人审闸门，以及 checkpointer 让中断后能从任意节点恢复。

---

## 已知限制（诚实清单）

- **部分云 provider 未实测**：`avatar:avatar-dashscope-wan-s2v` 的临时上传、提交、轮询和下载流程已通过本地模拟接口测试，
  但当前项目还没有百炼 Key 与业务空间 ID，尚未完成真实成片验证；`openai_images` / `comfy` / `http_job` / `minimax_h3`
  在代码里标了 `[未实测]`。
  首次使用请用 `python -m autovid step <步骤> --force` 单步调试。
- **当前音色走 CosyVoice2 云端克隆**。`edge_native` / `sapi` / `edge` 不支持克隆，
  严格流程不会自动切换到它们。
- **`still` 和本地 Wav2Lip 只保留作手动测试**：前者不会动嘴，后者只改嘴部，
  都不会被当前严格流程自动选中。
- **`metadata` 的 LLM 模式**改用 `openai_compat` 才生效，否则用脚本自带的标题。
- **`offline` 文案 provider 只保证结构正确**，内容质量必须靠 LLM 或你自己的文案。
- **未实现全自动发布**：这是刻意的，理由见上面的合规红线。
- **Web 端同时只允许一个运行**：ffmpeg 和显存都吃紧，并发跑只会互相拖垮。
- **`runs/` 会持续增长**：视频很占空间，记得 `python -m autovid purge --keep 5`。

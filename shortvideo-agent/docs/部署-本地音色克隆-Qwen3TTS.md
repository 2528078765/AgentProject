# 本地音色克隆部署指南：Qwen3-TTS（GGUF + ONNX）

> 目标：**免费、无需 API Key、数据不出本机**地克隆你自己的音色。
> 这条路在你机器上**可行**，但需要你亲自执行部署（原因见下）。

---

## 为什么是 Qwen3-TTS，而不是 HeyGem

| 项目 | 你机器能用吗 | 原因 |
|---|---|---|
| **HeyGem.ai**（数字人） | ❌ **不能** | 官方文档原文：*"Ensure you have an NVIDIA graphics card"*、*"The three services won't start without an NVIDIA graphics card"*。它是 Docker + PyTorch **CUDA**，ONNX 优化版仍走 CUDA EP。你的 RX 6750 GRE 是 AMD，没有 CUDA。 |
| **Qwen3-TTS-GGUF**（音色克隆） | ✅ **能** | Talker/Predictor 走 **llama.cpp + Vulkan**（AMD 可用），Encoder/Decoder 走 **ONNX Runtime DirectML**（AMD 可用），且**纯 CPU 也能跑（RTF 1.3）**。 |

关键区别：HeyGem 是「整条链路绑定 CUDA」，而 Qwen3-TTS-GGUF 是
「LLM 部分用 llama.cpp（跨平台）+ 音频编解码用 ONNX（可用 DirectML）」。

来源：
- [Qwen3-TTS 官方仓库](https://github.com/QwenLM/Qwen3-TTS)（开源，3 秒零样本克隆，0.6B/1.7B 双尺寸）
- [Qwen3-TTS-GGUF](https://github.com/HaujetZhao/Qwen3-TTS-GGUF)（llama.cpp 推理方案）

---

## 为什么这个部署要你自己跑

我这边的沙箱**写不了 pip 的临时文件**（`Permission denied ... *.whl.metadata`），
所以装不了 `modelscope` / `onnx` / `onnxruntime-directml` 这些依赖。

**你自己的终端没有这个限制**，直接跑就行。这也是唯一需要你动手的地方。

---

## 部署步骤

### 0. 先看资源账

| 项目 | 约占用 |
|---|---|
| Qwen3-TTS-0.6B 官方模型 | ~1.3 GB |
| 导出后的 GGUF + ONNX | ~1.5 GB |
| llama.cpp Vulkan 二进制 | ~50 MB |
| **合计** | **~3 GB** 磁盘 |
| 运行显存 | 0.6B 约 **1.3 GB**（你的 10G 绰绰有余） |
| 纯 CPU 运行 | 可以，RTF ≈ 1.3（50 秒视频约 65 秒合成） |

### 1. 部署 Qwen3-TTS-GGUF

```powershell
cd path\to\your-workspace
git clone https://github.com/HaujetZhao/Qwen3-TTS-GGUF.git
cd Qwen3-TTS-GGUF

# 装依赖。你是 AMD 卡 + 想用 DirectML，所以用 dml 这组
pip install uv
uv sync --extra dml

# 下载 llama.cpp 的 Vulkan 版（AMD 用这个，不是 CUDA 版）
# 从 https://github.com/ggml-org/llama.cpp/releases 找 b10621 版本
# 下载 llama-b10621-bin-win-vulkan-x64.zip，解压后把 DLL 放进 qwen3_tts_gguf/bin/
```

### 2. 下载官方模型（0.6B 就够）

```powershell
pip install modelscope
modelscope download --model Qwen/Qwen3-TTS-12Hz-0.6B-Base --local_dir ./model-base
```

> 用 **Base** 版本 —— 只有 Base 支持声音克隆。
> CustomVoice 是内置音色，VoiceDesign 是文字描述造音色。

### 3. 导出（按仓库文档跑一遍）

```powershell
python 11-Export-Codec-Encoder.py
python 12-Export-Speaker-Encoder.py
python 13-Export-Decoder.py
python 14-Export-Embeddings.py
python 15-Copy-Tokenizer.py
python 16-Quantize-ONNX-Models.py      # 转 FP16，DirectML 才快

python 21-Extract-Talker-Weights.py
python 22-Prepare-Talker-Tokenizer.py
python 23-Convert-Talker-GGUF.py
python 24-Quantize-Talker-GGUF.py

python 31-Extract-Predictor-Weights.py
python 32-Prepare-Predictor-Tokenizer.py
python 33-Convert-Predictor-GGUF.py
python 34-Quantize-Predictor-GGUF.py
```

### 4. 先用 GUI 验证能出声

```powershell
python 52-GUI.py
```

在里面选 **Base（声音克隆）**，喂一段你自己的录音，看能不能合出来。
**这一步必须成功再往下走** —— 后面都是接线工作。

---

## 接进 AutoVid

部署好之后告诉我，我写一个 `local_qwen_tts` provider 接上，大致是这样：

```python
# 伪代码示意
from qwen3_tts_gguf import TTSEngine, TTSConfig
engine = TTSEngine(model_dir=".../model-base")
stream = engine.create_stream()
stream.set_voice("你的参考音频.wav")      # ← 直接用资产库里的 ref.wav
result = stream.clone("要合成的文本", config=TTSConfig(seed=42))
result.save("seg_00.wav")
```

它和你现在用的 `http_json` provider 是同一个契约：
**「给一段文本 + 一段参考音频 → 返回一段音频」**。
所以接线只需要在 `autovid/providers.py` 里加一个函数，流水线其余部分一行都不用改
—— 音色/形象资产、断句气口、字幕、合成、发布包全部复用。

---

## 关于数字人（让嘴动）

HeyGem 在 AMD 上走不通。剩下三条路：

| 方案 | 可行性 | 说明 |
|---|---|---|
| **Wav2Lip ONNX + DirectML** | 勉强可行 | 模型小、能跑 AMD，但**许可为非商用**，画质也是几年前的 |
| **换 NVIDIA 显卡** | 最彻底 | 4GB 显存即可跑 HeyGem ONNX，你其他配置都够 |
| **云端数字人 API** | 最省事 | `avatar_http` provider 已经按通用契约留好了 |

我建议先把**音色克隆**跑通 —— 那是你视频里最核心的辨识度来源，
而且 Qwen3-TTS 这条路已经确认可行。数字人这块等你决定方向再动。

---

## 一个提醒：许可证

Qwen3-TTS 是阿里开源模型，商用前请看一下它的许可证条款
（通义系列通常允许商用但有一定条件）。
Wav2Lip 明确是**非商用**，别拿它做变现内容。

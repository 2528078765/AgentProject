"""音色资产与形象资产 —— 一等公民，不是每次现编。

为什么要单独做这一层：

1. **一次采集、长期复用**。音色和形象是资产，不该每做一条视频重新生成一次。
2. **天然参与 input_hash**。它们的 ID 写进 `config.project.voice_id / avatar_id`，
   而配置会进 manifest 并参与每一步的输入指纹 —— 所以换了音色或形象，
   下游步骤会自动失效重跑，不需要手动清缓存。
3. **采集时就做体检**。音频时长够不够、有没有音轨、照片分辨率够不够、
   是不是真的能解码 —— 这些问题必须在「采集」阶段就拦住，
   否则会拖到跑完 40 秒视频之后才炸。

目录结构：

    assets/voices/<voice_id>/
        meta.json      档案：名字、提示文本、时长、状态
        ref.wav        参考音频（统一转成 24kHz 单声道 PCM）
        preview.wav    可选试听
        model/         本地训练产物（GPT-SoVITS 权重等），可选
    assets/avatars/<avatar_id>/
        meta.json
        photos/*.jpg   原始照片
        preview.mp4    可选试看
        model/         LoRA / embedding 等，可选
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import media as M
from .errors import AutoVidError

# --------------------------------------------------------------------------- #
# 采集规范
# --------------------------------------------------------------------------- #
# 音色克隆对参考音频的要求：太短学不到音色，太长没必要且容易混入噪声
VOICE_MIN_SECONDS = 3.0
VOICE_GOOD_SECONDS = 10.0
VOICE_MAX_SECONDS = 180.0

# 形象照片：数字人驱动需要能看清五官，太小或太糊都不行
PHOTO_MIN_SIDE = 256
PHOTO_GOOD_SIDE = 512

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

VOICE_STATUS_EMPTY = "empty"       # 只有档案，还没录音
VOICE_STATUS_READY = "ready"       # 有可用参考音频，可以拿去克隆
VOICE_STATUS_TRAINED = "trained"   # 有本地训练产物

AVATAR_STATUS_EMPTY = "empty"
AVATAR_STATUS_READY = "ready"
AVATAR_STATUS_TRAINED = "trained"

# 录音文本：设计目标是覆盖常见中文声母、韵母和声调，同时保持自然聊天感。
# 控制在约二十多秒，既给克隆模型足够的音色信息，也不超过常见云接口的
# 三十秒参考音频限制。操作说明放在 VOICE_RECORD_TIPS，不混进转写文本。
VOICE_PROMPT_PARAGRAPHS: list[str] = [
    "大家好，今天想和你聊一件挺有意思的事。早上出门时阳光刚好，"
    "街边的树叶被风轻轻吹动，我顺手买了一杯热豆浆。",
    "你有没有发现，很多问题只要换个角度，就会出现新的答案。"
    "别着急，先把眼前这一步走稳，慢慢来，总会有新的收获。",
]

VOICE_RECORD_TIPS = [
    "找一个安静的房间，关掉空调和风扇，避开马路和键盘声。",
    "用手机自带录音机就行，离嘴 15～20 厘米，别贴太近，避免喷麦和呼吸声。",
    "保持平时聊天的语速和音量，不要刻意朗读，也不要压低声音。",
    "整段约 20～25 秒，读错时停一秒，再把这一整句重新读一遍。",
    "不要加背景音乐，也不要开降噪，原始干声效果最好。",
]

PHOTO_TIPS = [
    "建议拍 3～5 张：正脸、微左侧、微右侧、带笑容各一张。",
    "光线均匀，脸不要有强烈阴影；不要戴墨镜、口罩、帽子。",
    "背景尽量干净，上半身入镜，脸占画面 1/3 以上。",
    "分辨率至少 512×512，越大越好；不要用美颜过度或模糊的照片。",
    "总共一个人，不要合影 —— 模型会分不清该用哪张脸。",
]


def enrollment_script() -> str:
    return "\n".join(VOICE_PROMPT_PARAGRAPHS)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class VoiceAsset:
    id: str
    name: str
    created: str = ""
    status: str = VOICE_STATUS_EMPTY
    language: str = "zh-CN"
    prompt_text: str = ""
    ref_audio: str | None = None       # 相对 voice 目录的文件名
    ref_text: str = ""                 # 参考音频对应的文字（克隆必需）
    duration_s: float | None = None
    note: str = ""
    has_model: bool = False
    # 云端克隆出来的音色 ID，按 provider 名字缓存。
    # 关键：注册一次就够，绝不能每次生成视频都重新克隆一遍（既慢又费钱）。
    cloud_voice_ids: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VoiceAsset":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class AvatarAsset:
    id: str
    name: str
    created: str = ""
    status: str = AVATAR_STATUS_EMPTY
    photos: list[str] = field(default_factory=list)
    portrait: str | None = None        # 主形象照片（数字人驱动用这张）
    width: int | None = None
    height: int | None = None
    note: str = ""
    has_model: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AvatarAsset":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------- #
# 资产库
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id(prefix: str, seed: str) -> str:
    """生成纯 ASCII 的资产 ID —— 避免它出现在 URL / JSON 里时的编码麻烦。"""
    digest = hashlib.sha256(f"{prefix}:{seed}:{_now()}".encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{digest}"


def _safe_name(name: str, fallback: str) -> str:
    """把用户输入的名字清洗成安全文件名（保留中文）。"""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", (name or "").strip())
    return cleaned[:60] or fallback


class AssetStore:
    def __init__(self, config: Any):
        self.config = config
        self.root = config.path(config.get("project.assets_dir", "assets"))

    # ------------------------------------------------------------ 路径
    @property
    def voices_dir(self) -> Path:
        return self.root / "voices"

    @property
    def avatars_dir(self) -> Path:
        return self.root / "avatars"

    def voice_dir(self, voice_id: str) -> Path:
        return self.voices_dir / self._check_id(voice_id)

    def avatar_dir(self, avatar_id: str) -> Path:
        return self.avatars_dir / self._check_id(avatar_id)

    @staticmethod
    def _check_id(asset_id: str) -> str:
        """ID 来自 URL，必须防目录穿越。"""
        if not re.fullmatch(r"(voice|avatar)-[0-9a-f]{4,32}", asset_id or ""):
            raise AutoVidError(f"非法的资产 ID：{asset_id!r}")
        return asset_id

    # ============================================================ 音色
    def list_voices(self) -> list[VoiceAsset]:
        return self._load_all(self.voices_dir, VoiceAsset)

    def get_voice(self, voice_id: str) -> VoiceAsset | None:
        return self._load_one(self.voice_dir(voice_id) / "meta.json", VoiceAsset)

    def create_voice(self, name: str, language: str = "zh-CN") -> VoiceAsset:
        voice_id = _new_id("voice", name)
        target = self.voice_dir(voice_id)
        target.mkdir(parents=True, exist_ok=True)
        (target / "model").mkdir(exist_ok=True)
        asset = VoiceAsset(
            id=voice_id, name=name.strip() or "未命名音色", created=_now(),
            language=language, prompt_text=enrollment_script(),
        )
        self._save(target / "meta.json", asset.to_dict())
        return asset

    def save_voice_reference(
        self, voice_id: str, data: bytes, filename: str, ref_text: str = ""
    ) -> VoiceAsset:
        """保存参考音频：先体检，再统一转成 24kHz 单声道 PCM WAV。"""
        asset = self.get_voice(voice_id)
        if asset is None:
            raise AutoVidError(f"找不到音色 {voice_id}")
        target = self.voice_dir(voice_id)

        suffix = Path(filename or "").suffix.lower()
        if suffix not in AUDIO_EXTS:
            raise AutoVidError(
                f"不支持的音频格式 {suffix or '(无扩展名)'}；"
                f"支持：{', '.join(sorted(AUDIO_EXTS))}"
            )

        incoming = target / f"_incoming{suffix}"
        incoming.write_bytes(data)
        try:
            info = M.probe_media(incoming)
            if not info.get("has_audio"):
                raise AutoVidError(f"这个文件里没有音轨，无法作为音色样本（{info.get('error') or ''}）")
            duration = float(info.get("duration") or 0.0)
            if duration < VOICE_MIN_SECONDS:
                raise AutoVidError(
                    f"录音只有 {duration:.1f} 秒，太短了。"
                    f"至少 {VOICE_MIN_SECONDS:.0f} 秒，建议 {VOICE_GOOD_SECONDS:.0f} 秒以上。"
                )
            if duration > VOICE_MAX_SECONDS:
                raise AutoVidError(f"录音 {duration:.0f} 秒过长，请控制在 {VOICE_MAX_SECONDS:.0f} 秒内。")

            # 统一格式：克隆服务普遍要求干净的 wav，提前规范化能省掉一堆兼容问题
            ref = target / "ref.wav"
            M.run_ffmpeg(
                ["-i", str(incoming), "-ac", "1", "-ar", "24000",
                 "-c:a", "pcm_s16le", str(ref)],
                desc="规范化参考音频",
            )
        finally:
            incoming.unlink(missing_ok=True)

        asset.ref_audio = "ref.wav"
        asset.duration_s = round(duration, 2)
        asset.ref_text = (ref_text or "").strip() or enrollment_script()
        asset.status = VOICE_STATUS_READY
        asset.note = (
            "样本偏短，克隆稳定性一般" if duration < VOICE_GOOD_SECONDS
            else "参考音频已就绪"
        )
        self._save(target / "meta.json", asset.to_dict())
        return asset

    def delete_voice(self, voice_id: str) -> None:
        target = self.voice_dir(voice_id)
        if not target.is_dir():
            raise AutoVidError(f"找不到音色 {voice_id}")
        shutil.rmtree(target)

    def set_cloud_voice_id(self, voice_id: str, provider: str, cloud_id: str) -> VoiceAsset:
        """记住某个云端 provider 为这个音色分配的音色 ID，避免重复克隆。"""
        asset = self.get_voice(voice_id)
        if asset is None:
            raise AutoVidError(f"找不到音色 {voice_id}")
        asset.cloud_voice_ids = dict(asset.cloud_voice_ids or {})
        asset.cloud_voice_ids[provider] = cloud_id
        self._save(self.voice_dir(voice_id) / "meta.json", asset.to_dict())
        return asset

    def clear_cloud_voice_ids(self, voice_id: str) -> VoiceAsset:
        """清掉云端音色缓存（换了账号/换了好莱坞的参考音频后需要重注册）。"""
        asset = self.get_voice(voice_id)
        if asset is None:
            raise AutoVidError(f"找不到音色 {voice_id}")
        asset.cloud_voice_ids = {}
        self._save(self.voice_dir(voice_id) / "meta.json", asset.to_dict())
        return asset

    def voice_reference(self, voice_id: str) -> Path | None:
        """返回参考音频的绝对路径（供克隆 provider 使用）。"""
        asset = self.get_voice(voice_id)
        if asset is None or not asset.ref_audio:
            return None
        path = self.voice_dir(voice_id) / asset.ref_audio
        return path if path.exists() else None

    # ============================================================ 形象
    def list_avatars(self) -> list[AvatarAsset]:
        return self._load_all(self.avatars_dir, AvatarAsset)

    def get_avatar(self, avatar_id: str) -> AvatarAsset | None:
        return self._load_one(self.avatar_dir(avatar_id) / "meta.json", AvatarAsset)

    def create_avatar(self, name: str) -> AvatarAsset:
        avatar_id = _new_id("avatar", name)
        target = self.avatar_dir(avatar_id)
        (target / "photos").mkdir(parents=True, exist_ok=True)
        (target / "model").mkdir(exist_ok=True)
        asset = AvatarAsset(id=avatar_id, name=name.strip() or "未命名形象", created=_now())
        self._save(target / "meta.json", asset.to_dict())
        return asset

    def save_avatar_photo(
        self, avatar_id: str, data: bytes, filename: str, make_primary: bool = False
    ) -> AvatarAsset:
        asset = self.get_avatar(avatar_id)
        if asset is None:
            raise AutoVidError(f"找不到形象 {avatar_id}")
        target = self.avatar_dir(avatar_id)
        photos_dir = target / "photos"

        suffix = Path(filename or "").suffix.lower()
        if suffix not in IMAGE_EXTS:
            raise AutoVidError(
                f"不支持的图片格式 {suffix or '(无扩展名)'}；"
                f"支持：{', '.join(sorted(IMAGE_EXTS))}"
            )

        stem = _safe_name(Path(filename).stem, "photo")
        # 避免同名覆盖：追加序号
        index = 1
        candidate = f"{stem}{suffix}"
        while (photos_dir / candidate).exists():
            index += 1
            candidate = f"{stem}_{index}{suffix}"
        photo_path = photos_dir / candidate
        photo_path.write_bytes(data)

        info = M.probe_media(photo_path)
        if not info.get("has_video"):
            photo_path.unlink(missing_ok=True)
            raise AutoVidError(f"这个文件不是有效图片（{info.get('error') or '无法解码'}）")
        width, height = int(info.get("width") or 0), int(info.get("height") or 0)
        if min(width, height) < PHOTO_MIN_SIDE:
            photo_path.unlink(missing_ok=True)
            raise AutoVidError(
                f"图片分辨率只有 {width}×{height}，太小了（最短边至少 {PHOTO_MIN_SIDE}px，"
                f"建议 {PHOTO_GOOD_SIDE}px 以上）。"
            )

        asset.photos.append(candidate)
        if make_primary or not asset.portrait:
            asset.portrait = candidate
            asset.width, asset.height = width, height
        asset.status = AVATAR_STATUS_READY
        asset.note = f"{len(asset.photos)} 张照片"
        self._save(target / "meta.json", asset.to_dict())
        return asset

    def set_primary_photo(self, avatar_id: str, filename: str) -> AvatarAsset:
        asset = self.get_avatar(avatar_id)
        if asset is None:
            raise AutoVidError(f"找不到形象 {avatar_id}")
        if filename not in asset.photos:
            raise AutoVidError(f"照片 {filename} 不属于这个形象")
        path = self.avatar_dir(avatar_id) / "photos" / filename
        info = M.probe_media(path)
        asset.portrait = filename
        asset.width = int(info.get("width") or 0) or None
        asset.height = int(info.get("height") or 0) or None
        self._save(self.avatar_dir(avatar_id) / "meta.json", asset.to_dict())
        return asset

    def delete_avatar_photo(self, avatar_id: str, filename: str) -> AvatarAsset:
        asset = self.get_avatar(avatar_id)
        if asset is None:
            raise AutoVidError(f"找不到形象 {avatar_id}")
        if filename not in asset.photos:
            raise AutoVidError(f"照片 {filename} 不属于这个形象")
        (self.avatar_dir(avatar_id) / "photos" / _safe_name(Path(filename).stem, "photo")
         ).with_suffix(Path(filename).suffix).unlink(missing_ok=True)
        asset.photos = [p for p in asset.photos if p != filename]
        if asset.portrait == filename:
            asset.portrait = asset.photos[0] if asset.photos else None
        asset.status = AVATAR_STATUS_READY if asset.photos else AVATAR_STATUS_EMPTY
        self._save(self.avatar_dir(avatar_id) / "meta.json", asset.to_dict())
        return asset

    def delete_avatar(self, avatar_id: str) -> None:
        target = self.avatar_dir(avatar_id)
        if not target.is_dir():
            raise AutoVidError(f"找不到形象 {avatar_id}")
        shutil.rmtree(target)

    def avatar_portrait(self, avatar_id: str) -> Path | None:
        """返回主形象照片的绝对路径（数字人驱动用这张）。"""
        asset = self.get_avatar(avatar_id)
        if asset is None or not asset.portrait:
            return None
        path = self.avatar_dir(avatar_id) / "photos" / asset.portrait
        return path if path.exists() else None

    def avatar_photo_path(self, avatar_id: str, filename: str) -> Path | None:
        """按文件名取照片，带目录穿越防护（前端要用它显示缩略图）。"""
        asset = self.get_avatar(avatar_id)
        if asset is None or filename not in asset.photos:
            return None
        path = self.avatar_dir(avatar_id) / "photos" / filename
        return path if path.exists() else None

    # ============================================================ 概览
    def summary(self) -> dict[str, Any]:
        voices = self.list_voices()
        avatars = self.list_avatars()
        return {
            "root": str(self.root),
            "voices": [v.to_dict() for v in voices],
            "avatars": [a.to_dict() for a in avatars],
            "voice_prompt": enrollment_script(),
            "voice_tips": VOICE_RECORD_TIPS,
            "photo_tips": PHOTO_TIPS,
            "rules": {
                "voice_min_s": VOICE_MIN_SECONDS,
                "voice_good_s": VOICE_GOOD_SECONDS,
                "photo_min_side": PHOTO_MIN_SIDE,
                "audio_exts": sorted(AUDIO_EXTS),
                "image_exts": sorted(IMAGE_EXTS),
            },
        }

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _save(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _load_one(path: Path, cls: Any) -> Any:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        asset = cls.from_dict(data)
        # 目录被手工删过时，把状态纠正回来，避免前端显示「就绪」但文件其实没了
        base = path.parent
        if isinstance(asset, VoiceAsset):
            if asset.ref_audio and not (base / asset.ref_audio).exists():
                asset.ref_audio = None
                asset.status = VOICE_STATUS_EMPTY
        else:
            asset.photos = [p for p in asset.photos if (base / "photos" / p).exists()]
            if asset.portrait and asset.portrait not in asset.photos:
                asset.portrait = asset.photos[0] if asset.photos else None
            if not asset.photos:
                asset.status = AVATAR_STATUS_EMPTY
        asset.has_model = (base / "model").is_dir() and any((base / "model").iterdir())
        if asset.has_model:
            asset.status = (VOICE_STATUS_TRAINED if isinstance(asset, VoiceAsset)
                            else AVATAR_STATUS_TRAINED)
        return asset

    @classmethod
    def _load_all(cls, directory: Path, asset_cls: Any) -> list[Any]:
        if not directory.exists():
            return []
        items: list[Any] = []
        for child in sorted(directory.iterdir()):
            if not child.is_dir():
                continue
            asset = cls._load_one(child / "meta.json", asset_cls)
            if asset is not None:
                items.append(asset)
        items.sort(key=lambda a: a.created, reverse=True)
        return items

"""开餐曲：跑搭配的时候放一段「看见什么吃什么」的开场小曲，先把食欲叫起来。

版权说明（重要）：
- 真正的《看见什么吃什么》是有版权的商业录音，本项目**不分发、不内嵌**它。
- 默认播放的是本模块用正弦波合成的**原创开餐小段**（五声音阶、上扬动机，
  不含任何第三方旋律），因此可以随 skill 一起分发。
- 想听原曲：把你自己合法持有的音频文件路径给 `SEEANDEAT_ANTHEM`，
  播放时会优先用它，歌词/旋律都由你本地的文件提供，本项目不参与分发。

关掉：`--no-sound`，或 `export SEEANDEAT_NO_ANTHEM=1`。
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

SONG_TITLE = "看见什么吃什么"

# 五声音阶（G 大调宫调式），频率单位 Hz
_FREQ = {
    "G4": 392.00, "A4": 440.00, "C5": 523.25, "D5": 587.33,
    "E5": 659.25, "G5": 783.99, "A5": 880.00,
}

# (音名, 拍长秒)。原创动机：两小节上扬 + 一句收束，约 3 秒。
RIFF: list[tuple[str, float]] = [
    ("G4", 0.13), ("G4", 0.13), ("A4", 0.13), ("C5", 0.26),
    ("C5", 0.13), ("D5", 0.13), ("E5", 0.45),
    ("D5", 0.13), ("C5", 0.13), ("D5", 0.13), ("G5", 0.45),
    ("E5", 0.13), ("D5", 0.13), ("C5", 0.13), ("A4", 0.13), ("G4", 0.55),
]
# 鼓点：每 0.52 秒来一记低频"咚"，撑起节奏感
_KICK_INTERVAL = 0.52

SAMPLE_RATE = 22050

BANNER = r"""
   ♪  看 见 什 么 吃 什 么   ♪
  ┌───────────────────────────┐
  │  今日份纠结，到此为止     │
  │  菜单已读，开饭           │
  └───────────────────────────┘
""".strip("\n")


def _env(t: float, dur: float) -> float:
    """快起慢落的包络，避免爆音。"""
    attack = 0.008
    if t < attack:
        return t / attack
    return math.exp(-3.2 * (t - attack) / max(dur, 0.01))


def _tone(t: float, freq: float, dur: float) -> float:
    if t >= dur:
        return 0.0
    e = _env(t, dur)
    # 基频 + 少量二三次谐波，听起来像玩具钢琴而非纯正弦
    v = (
        math.sin(2 * math.pi * freq * t)
        + 0.22 * math.sin(2 * math.pi * freq * 2 * t)
        + 0.10 * math.sin(2 * math.pi * freq * 3 * t)
    )
    return v * e


def _kick(t: float, phase: float) -> float:
    if t >= 0.16:
        return 0.0
    f = 70.0 - 30.0 * (t / 0.16)
    return math.sin(2 * math.pi * f * t) * math.exp(-14.0 * t) * 0.35 * phase


def render_samples() -> list[float]:
    total = sum(d for _, d in RIFF) + 0.25
    n = int(total * SAMPLE_RATE)
    buf = [0.0] * n
    # 旋律
    pos = 0.0
    for name, dur in RIFF:
        f = _FREQ.get(name, 440.0)
        steps = int((dur + 0.02) * SAMPLE_RATE)
        for i in range(steps):
            idx = int(pos * SAMPLE_RATE) + i
            if idx >= n:
                break
            buf[idx] += _tone(i / SAMPLE_RATE, f, dur)
        pos += dur
    # 鼓点
    k = 0.0
    while k < total:
        base = int(k * SAMPLE_RATE)
        for i in range(int(0.16 * SAMPLE_RATE)):
            idx = base + i
            if idx >= n:
                break
            buf[idx] += _kick(i / SAMPLE_RATE, 1.0)
        k += _KICK_INTERVAL
    # 归一化 + 软削顶
    peak = max((abs(v) for v in buf), default=1.0) or 1.0
    scale = 0.72 / peak
    return [max(-1.0, min(1.0, v * scale)) for v in buf]


def cache_path() -> Path:
    d = Path(os.environ.get("SEEANDEAT_CACHE_DIR") or (Path.home() / ".mcd-see-and-eat"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "anthem.wav"


def synthesize(path: Path | None = None, force: bool = False) -> Path:
    """合成开餐小段，返回 WAV 路径（已存在则复用）。"""
    path = Path(path) if path else cache_path()
    if path.exists() and path.stat().st_size > 1024 and not force:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = render_samples()
    tmp = Path(tempfile.mkdtemp()) / "anthem.wav"
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"".join(struct.pack("<h", int(s * 32767)) for s in samples))
    shutil.move(str(tmp), str(path))
    return path


# ------------------------------------------------------------------ 播放
def _player() -> list[str] | None:
    if sys.platform == "darwin" and shutil.which("afplay"):
        return ["afplay"]
    for cmd in ("paplay", "aplay", "ffplay"):
        if shutil.which(cmd):
            return [cmd, "-nodisp", "-autoexit", "-loglevel", "quiet"] if cmd == "ffplay" else [cmd]
    if os.name == "nt":
        return ["powershell", "-NoProfile", "-Command"]
    return None


def user_song() -> Path | None:
    """用户自己指定的原曲文件（最高优先级）。"""
    v = os.environ.get("SEEANDEAT_ANTHEM", "").strip()
    if not v:
        return None
    p = Path(v).expanduser()
    return p if p.exists() else None


def enabled(flag: bool = True) -> bool:
    if os.environ.get("SEEANDEAT_NO_ANTHEM", "").strip() in ("1", "true", "yes"):
        return False
    return flag


def play(flag: bool = True, wait: bool = False) -> str:
    """放歌。返回一句给用户看的说明。

    默认后台播放：配餐是前台正事，音乐不该拖慢它。
    """
    if not enabled(flag):
        return "开餐曲：已关闭"
    song = user_song()
    try:
        if song is not None:
            target, what = song, f"你自己指定的《{song.name}》"
        else:
            target, what = synthesize(), f"{SONG_TITLE}·原创开餐小段（合成，无第三方旋律）"
    except Exception as exc:  # 合成失败也不能影响配餐
        return f"开餐曲：合成失败（{exc}），已跳过"

    pl = _player()
    if pl is None:
        return f"开餐曲：{target} 已生成，但系统没有可用的播放器（macOS 用 afplay）"
    try:
        if pl[0] == "powershell":
            cmd = pl + [f'(New-Object Media.SoundPlayer "{target}").PlaySync()']
        else:
            cmd = pl + [str(target)]
        if wait:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20, check=False)
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception as exc:
        return f"开餐曲：播放失败（{exc}）"
    return f"开餐曲：正在播放 {what}"


def song_hint() -> str:
    """想听原曲怎么配。"""
    return (
        f"想换成原曲《{SONG_TITLE}》：把你的音频文件路径给它 —— "
        f"export SEEANDEAT_ANTHEM=/path/to/{SONG_TITLE}.m4a"
    )


def usage_lines() -> list[str]:
    return [
        BANNER,
        f"· 开餐曲：默认播放原创合成小段（约 3 秒，不含第三方旋律）",
        f"· {song_hint()}",
        "· 不想听：加 --no-sound 或 export SEEANDEAT_NO_ANTHEM=1",
    ]


# ------------------------------------------------------------------ 给 HTML 报告用
def notes_js() -> str:
    """把同一段动机交给浏览器 WebAudio 播放（报告页上的「开餐曲」按钮）。"""
    rows = ",".join(f'["{n}",{d}]' for n, d in RIFF)
    return (
        f"const ANTHEM_TITLE={SONG_TITLE!r};"
        f"const ANTHEM_NOTES=[{rows}];"
        f"const ANTHEM_FREQ={_FREQ!r};".replace("'", '"')
    )

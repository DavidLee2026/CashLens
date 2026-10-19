"""输入来源标签：打字进来的不许显示成「语音」（2026-10-19 立）

现象：David 手动打了三笔收入，界面上「来源」那一栏显示**语音**。查证：
① 界面根本没有语音输入（无 MediaRecorder / 无录音按钮）；
② 提示词把 `voice` 列成了 channel 的可选值，模型在「说一句话记账」的语境下猜成了 voice；
③ 规则解析那条路给的是 manual / wechat —— 同一句话换解析方式，来源就变了。

口径：**输入途径服务端比模型清楚**。模型说 voice 一律落 manual；
微信 / 支付宝 / 现金 / 银行卡 是付款渠道（文字里真说了），保留。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.main import _text_channel  # noqa: E402


def test_model_guessed_voice_becomes_manual():
    assert _text_channel("voice") == "manual"
    assert _text_channel("") == "manual"
    assert _text_channel(None) == "manual"
    assert _text_channel("   ") == "manual"


def test_payment_channels_are_kept():
    for ch in ("wechat", "alipay", "bank", "cash"):
        assert _text_channel(ch) == ch


def test_unknown_channel_passes_through_untouched():
    """模型给了别的值就原样保留 —— 这条只负责「voice 不是打字」，不做白名单。"""
    assert _text_channel("receipt") == "receipt"


def test_chat_draft_path_uses_the_normalizer():
    """护栏：模型分支必须走这个归一化，不许直接把 a.get("channel") 塞进草稿。"""
    src = (Path(__file__).resolve().parents[1] / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    assert "_text_channel(a.get(\"channel\"))" in src
    assert "_draft_one(direction, amount, a.get(\"category\"), a.get(\"channel\")" not in src

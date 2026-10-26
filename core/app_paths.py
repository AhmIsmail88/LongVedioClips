# -*- coding: utf-8 -*-
"""مسارات التطبيق: جذر الكود مقابل مجلد بيانات المستخدم.

عند التشغيل من المصدر كل شيء داخل المستودع. وعند التغليف تُحفظ حالة التشغيل
(المشاريع، القوائم، التحميلات) في ``%LOCALAPPDATA%\\MomentForgeAI`` بدلًا من
مجلد الحزمة ``_internal``، حتى لا تُمحى مع كل تحديث أو إعادة بناء.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FROZEN = bool(getattr(sys, "frozen", False))


def data_root() -> Path:
    """مجلد بيانات المستخدم القابل للكتابة."""
    if not FROZEN:
        return REPO_ROOT
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MomentForgeAI"
    base.mkdir(parents=True, exist_ok=True)
    return base


DATA_ROOT = data_root()

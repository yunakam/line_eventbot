# events/policies.py
from enum import Enum
from typing import Optional

class EditPolicy(str, Enum):
    AUTHOR_ONLY = "author_only"
    MEMBERS = "members"

def get_scope_edit_policy(scope_id: Optional[str]) -> EditPolicy:
    """
    当面は固定値。将来はDB（GroupPolicy）や設定値から取得する想定。
    """
    return EditPolicy.AUTHOR_ONLY

def can_edit_event(user_id: str, event) -> bool:
    """
    編集可能かを判定する。event は Event インスタンス想定。
    """
    policy = get_scope_edit_policy(getattr(event, "scope_id", None) or "")
    if policy == EditPolicy.MEMBERS:
        return True
    return getattr(event, "created_by", None) == user_id

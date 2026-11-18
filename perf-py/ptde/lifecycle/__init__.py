"""PTDE 生命周期层：Dataset 状态机（R0）；Definition/Instance 管理与清理（R6）。"""

from ptde.lifecycle.state_machine import InvalidStateTransition, transition

__all__ = ["InvalidStateTransition", "transition"]

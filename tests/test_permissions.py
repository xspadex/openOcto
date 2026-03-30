"""Tests for the permission system (roles, ACL, allow/deny overrides)."""

import pytest
from openocto.permissions import resolve_allowed_ops, check_permission, ROLE_OPS


# ---- resolve_allowed_ops ----

class TestResolveAllowedOps:
    def test_full_role(self):
        ops = resolve_allowed_ops("full")
        assert "shell" in ops
        assert "kill" in ops
        assert "cat" in ops
        assert "edit" in ops
        assert "metrics" in ops

    def test_readwrite_role(self):
        ops = resolve_allowed_ops("readwrite")
        assert "cat" in ops
        assert "edit" in ops
        assert "metrics" in ops
        assert "shell" not in ops
        assert "kill" not in ops

    def test_readonly_role(self):
        ops = resolve_allowed_ops("readonly")
        assert "cat" in ops
        assert "glob" in ops
        assert "grep" in ops
        assert "metrics" in ops
        assert "edit" not in ops
        assert "shell" not in ops

    def test_none_role(self):
        ops = resolve_allowed_ops("none")
        assert len(ops) == 0

    def test_unknown_role_returns_empty(self):
        ops = resolve_allowed_ops("nonexistent")
        assert len(ops) == 0

    def test_allow_override(self):
        ops = resolve_allowed_ops("readonly", allow=["shell"])
        assert "shell" in ops
        assert "cat" in ops

    def test_deny_override(self):
        ops = resolve_allowed_ops("full", deny=["shell", "kill"])
        assert "shell" not in ops
        assert "kill" not in ops
        assert "cat" in ops

    def test_allow_and_deny_together(self):
        # deny takes precedence over role, allow adds
        ops = resolve_allowed_ops("readonly", allow=["edit"], deny=["cat"])
        assert "edit" in ops
        assert "cat" not in ops
        assert "glob" in ops

    def test_metrics_in_all_non_none_roles(self):
        for role in ("full", "readwrite", "readonly"):
            ops = resolve_allowed_ops(role)
            assert "metrics" in ops, f"metrics missing from {role}"


# ---- check_permission ----

class TestCheckPermission:
    def test_no_acl_allows_all(self):
        allowed, reason = check_permission(None, "anyone", "shell")
        assert allowed is True

    def test_no_acl_empty_dict_allows_all(self):
        # Empty dict is falsy → treated same as None (no ACL = allow all)
        allowed, reason = check_permission({}, "anyone", "shell")
        assert allowed is True

    def test_default_role_applied(self):
        acl = {"default": "readonly"}
        allowed, _ = check_permission(acl, "unknown_device", "cat")
        assert allowed is True

    def test_default_role_blocks(self):
        acl = {"default": "readonly"}
        allowed, reason = check_permission(acl, "unknown_device", "shell")
        assert allowed is False
        assert "readonly" in reason

    def test_specific_device_role(self):
        acl = {
            "default": "readonly",
            "alice_phone": "full",
        }
        allowed, _ = check_permission(acl, "alice_phone", "shell")
        assert allowed is True

    def test_specific_device_with_deny(self):
        acl = {
            "default": "none",
            "bob_laptop": {"role": "readwrite", "deny": ["edit"]},
        }
        allowed, _ = check_permission(acl, "bob_laptop", "cat")
        assert allowed is True
        allowed, _ = check_permission(acl, "bob_laptop", "edit")
        assert allowed is False

    def test_specific_device_with_allow(self):
        acl = {
            "default": "none",
            "ci_runner": {"role": "readonly", "allow": ["shell"]},
        }
        allowed, _ = check_permission(acl, "ci_runner", "shell")
        assert allowed is True
        allowed, _ = check_permission(acl, "ci_runner", "edit")
        assert allowed is False

    def test_unknown_device_falls_to_default(self):
        acl = {
            "default": "readwrite",
            "alice_phone": "full",
        }
        allowed, _ = check_permission(acl, "random_device", "edit")
        assert allowed is True
        allowed, _ = check_permission(acl, "random_device", "shell")
        assert allowed is False

    def test_default_none_blocks_everything(self):
        acl = {"default": "none"}
        for op in ROLE_OPS["full"]:
            allowed, _ = check_permission(acl, "device", op)
            assert allowed is False, f"{op} should be blocked"

"""Permission system for openOcto networks.

Defines role-based access control with three tiers:
  - full: shell access (complete control)
  - readwrite: cat, glob, grep, edit (structured tools only)
  - readonly: cat, glob, grep (can only look)

Roles can be customized with --allow/--deny to add/remove specific ops.
"""

# Operation sets for each role
ROLE_OPS = {
    "full": {
        "shell", "kill", "cat", "edit", "glob", "grep",
        "clipboard_write", "clipboard_read",
        "transfer_download", "inbox_receive", "inbox_list",
        "metrics",
    },
    "readwrite": {"cat", "edit", "glob", "grep", "inbox_list", "metrics"},
    "readonly": {"cat", "glob", "grep", "metrics"},
    "none": set(),
}

# All known operations (for validation)
ALL_OPS = ROLE_OPS["full"]


def resolve_allowed_ops(role, allow=None, deny=None):
    """Given a role + optional allow/deny overrides, return the set of permitted ops."""
    ops = set(ROLE_OPS.get(role, set()))
    if allow:
        ops |= set(allow)
    if deny:
        ops -= set(deny)
    return ops


def check_permission(acl_data, requester, task_type):
    """Check if requester can execute task_type on this terminal.

    Args:
        acl_data: dict from Redis, e.g.:
            {"default": "readonly", "bob_phone": {"role": "readwrite", "deny": ["edit"]}}
        requester: identity name of the device submitting the task
        task_type: the operation type (shell, cat, edit, etc.)

    Returns:
        (allowed: bool, reason: str)
    """
    if not acl_data:
        # No ACL configured = allow all (backward compat)
        return True, "ok"

    # Check specific ACL entry for requester
    entry = acl_data.get(requester)
    if entry:
        if isinstance(entry, str):
            # Simple role string, e.g. "readwrite"
            role, allow, deny = entry, None, None
        else:
            # Dict with role + optional allow/deny
            role = entry.get("role", "readonly")
            allow = entry.get("allow")
            deny = entry.get("deny")
        ops = resolve_allowed_ops(role, allow, deny)
        if task_type in ops:
            return True, "ok"
        return False, f"'{requester}' cannot '{task_type}' (role: {role})"

    # Fall back to default role
    default_role = acl_data.get("default", "none")
    ops = resolve_allowed_ops(default_role)
    if task_type in ops:
        return True, "ok"
    return False, f"'{requester}' cannot '{task_type}' (default role: {default_role})"

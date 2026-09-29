---
name: openocto-jump-hosts
description: Discovers SSH targets behind OpenOcto jump terminals and executes non-interactive commands through them. Use when the user asks which servers an OpenOcto terminal can access, wants to inspect a jump terminal's SSH config, selects an SSH target, or runs a command through the OpenOcto-to-SSH chain.
---

# Cursor adapter

Before performing this workflow, read and follow the canonical Agent Skill at:

`../../../.agents/skills/openocto-jump-hosts/SKILL.md`

Prefer available OpenOcto MCP tools in Cursor. If they are unavailable, use the
canonical skill's authenticated `octo` CLI fallback.

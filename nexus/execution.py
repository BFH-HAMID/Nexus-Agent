"""Conservative execution intent and evidence labels, not model success scoring."""
from __future__ import annotations

import re

# Only imperative task lines: questions, plans and quoted code remain legitimate
# text answers. This is a UX guard, not a semantic proof of arbitrary task intent.
_ACTION = re.compile(
    r'(?im)^\s*(?:(?:task|goal|request)\s*:\s*)?'
    r'(?:(?:please|can you|could you|i want you to)\s+)*'
    r'(?:implement|fix|repair|debug|refactor|update|modify|edit|create|add|remove|delete|'
    r'build|install|run|execute|commit|push|deploy|write)\b'
)
_ADVICE = re.compile(
    r'(?i)^\s*(?:(?:please|can you|could you)\s+)*(?:'
    r'(?:explain|what|why|how|describe|suggest|plan|design|show me|tell me)\b|'
    r'(?:write|create|draft)\s+(?:(?:a|an|the|me|short|brief|detailed)\s+)*'
    r'(?:plan|example|explanation|poem|story|email|message|outline|summary|pseudocode)\b)'
)
_CLAIM = re.compile(
    r'(?i)\b(?:files? (?:were |was |have been )?(?:created|changed|modified|updated)|'
    r'(?:i have|i\'ve|i) (?:created|changed|modified|updated|implemented|fixed|committed|pushed))\b'
)
WORKSPACE_TOOLS = frozenset({'list_files', 'read_file', 'grep', 'write_file', 'edit_file',
                             'delete_file', 'run_command', 'git', 'git_prepare', 'gh', 'gh_create_pr', 'gh_merge_pr'})


_LOCAL_ACTION = re.compile(
    r'(?i)\b(?:fix|fixed|implement|update|push|commit|deploy)\s+(?:koro|kore|korun)\b|'
    r'(?:ঠিক\s*কর|বাগ\s*ফিক্স|কোড\s*লিখে|পুশ\s*কর)'
)


def needs_execution(task: str) -> bool:
    text = re.sub(r'```[\s\S]*?```', '', task)
    return not _ADVICE.match(text) and bool(_ACTION.search(text) or _LOCAL_ACTION.search(text))


def claims_execution(text: str) -> bool:
    text = re.sub(r"(?i)\bno files? (?:were |was |have been )?(?:created|changed|modified|updated)\b", "", text)
    return bool(_CLAIM.search(text))

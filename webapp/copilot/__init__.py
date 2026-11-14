"""SECS Copilot: the generative / agentic AI module of the web system (docs/system/COPILOT_EN.md).

The model (Claude, through the official Anthropic SDK) understands the request, explains and writes; every number,
plate and record comes from the system's own service functions, called as tools with the signed-in user's
permissions, masking and audit. This semester it is read-only (plan Rev2, config.ACTIONS_ENABLED): it changes no
record and prepares no change. Without a key or network the module falls back to template answers that are labelled
as such.

    config.py     model, effort, limits, prices; online / offline status; ACTIONS_ENABLED (False: read-only)
    llm.py        the Anthropic SDK adapter (streaming, prompt caching, refusal fallback) and a scripted fake for tests
    prompts.py    the frozen system prompt, the role sections, the per-turn context
    tools.py      the tools: wrappers of webapp/services, input schemas, citations, the prepare_* action tools (off)
    agent.py      the chat loop: stream, run tools, store the transcript, check the numbers, log the cost
    actions.py    action cards (dormant: refused while read-only): prepared write operations the user confirms
    numbers.py    the number check: every number of an answer must be found in the tool results
    explain.py    "AI explain" of an alert: how it was computed from the stored values, the SOP and the legal refs
    sop.py        the reviewed SOP file (webapp/sop.yaml)
    documents.py  generated documents: case summary for the evidence pack, daily traffic and congestion review
    vlm.py        optional image checks (off by default): second reading of provisional plates, appearance check
    offline.py    quick questions and template answers (offline mode: templates, not AI)
    store.py      conversations, actions, documents, usage; retention
"""

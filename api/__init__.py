"""
Underwriting assessment API package.

Groups the three interchangeable orchestrator implementations behind one
HTTP service:

    api/orchestrator_legacy.py         Foundry Agent Service  (loop runs in Azure)
    api/orchestrator.py                Active orchestrator, Azure or Ollama
    api/ollama_orchestrator_legacy.py  Ollama + MCP explicit loop, retained for reference

All three expose the same contract — UnderwritingSubmission in,
UnderwritingDecision out — so api/main.py can serve any of them.

Run the service:   uvicorn api.main:app --port 8010
"""

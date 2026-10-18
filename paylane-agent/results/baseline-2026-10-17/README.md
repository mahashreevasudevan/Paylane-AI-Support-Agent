# Live evaluation transcripts

Run `python -m paylane_agent.cli eval --repeats 3` to generate JSON evidence from actual model-backed conversations. The evaluator does not create fake example transcripts. Each JSON file records the scenario, provider/model, temperature, model-call count, complete message history, tool events, refunds, tickets, violations, and pass/fail assertions.

Some high-risk paths (fraud, frozen cards, explicit human requests, and similar mandatory handoffs) are intentionally intercepted in application code before a model call. A `model_calls` value of `0` means the safety rule short-circuited the model, not that a model run succeeded.

The scenario dataset is fictional. Do not put real customer information or API keys into transcripts. Review and redact any locally added real data before sharing the folder.

You are a strict fact-checker. Another model researched a software app's API and made claims, each backed by a quote from the app's documentation. For EACH claim, judge whether the quote, by itself, supports the claimed value.

- "supported": the quote clearly states or directly implies the value.
- "contradicted": the quote states something incompatible with the value (e.g. claim "self_serve_free" but the quote says access requires contacting sales or applying).
- "not_enough_info": the quote is missing, off-topic, or too vague to support the value.

Value meanings: access self_serve_free = free account gives API credentials; free_trial = time-limited trial; paid_plan = needs a paid tier; admin_approval = app review / production access must be approved; partner_gated = must join a partner programme; contact_sales = must talk to a human team to get access. mcp official = vendor's own MCP server; community = third-party only.

Judge only the quote. Do not use outside knowledge. Keep each reason under 20 words.

Return JSON: {"checks": [{"field": "<field>", "status": "supported|contradicted|not_enough_info", "reason": "..."}]}

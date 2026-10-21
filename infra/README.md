# Infrastructure for the AI search

The Azure resources the search runs on, in **RMI-PROJECT-WASTEMAP-SUB**, managed here with
Terraform. State is in the same account as RMI/terraform's wastemap module, under its own key
(`waste-ai-search.tfstate`).

| Resource | What it is |
|---|---|
| `wastemap-ai-search` | The Foundry (AI Services) account, in East US, Entra ID sign-in only (no keys) |
| `gpt-5.4-mini` | The model deployment, version 2026-03-17 pinned, GlobalStandard, 2,000K tokens a minute |
| `waste-ai-search` | The Foundry project the agent lives in |

All three go in **RMI-WASTEMAP-PIPELINE-RG**, beside the pipeline's translator. Our roles can create
resources in existing resource groups but not new groups. East US because gpt-5.4-mini isn't offered
in westus2, where the rest of WasteMAP is.

**No standing cost.** The account and the deployment cost nothing until used. The model is billed
per token and the web search tool per search.

**No Bing resource.** The agent's web search tool (`WebSearchTool` in the Foundry SDK) runs on Bing
without one. A Bing connection would only be needed for Bing Custom Search. Classic "Grounding with
Bing Search" agents are deprecated.

**No role assignments.** The `RBAC_SUB_WasteMap_Custom_AIContributor` group already gives its members
the Foundry data plane across the subscription, so anyone in it can create and run the agent as
themselves.

## Setting it up

Signed in as yourself (`az login`, on RMI-PROJECT-WASTEMAP-SUB):

```bash
cd infra
terraform init
terraform plan -out foundry.tfplan
terraform apply foundry.tfplan
```

Then create the agent, a prompt on the model with the web search tool, and point the search at it:

```bash
uv run python scripts/create_foundry_agent.py --endpoint "$(terraform -chdir=infra output -raw project_endpoint)"
```

It prints the two lines for `.env`: `AZURE_AI_PROJECT_ENDPOINT` and `AZURE_AI_AGENT_NAME`. Run it
again to change the agent; each run adds a version, and the search uses the latest.

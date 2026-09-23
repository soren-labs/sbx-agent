import { renderAccounts } from "./accounts.js";
import { renderAgent } from "./agent.js";
import { renderAgents } from "./agents.js";
import { renderArtifact, renderArtifacts } from "./artifacts.js";
import { renderCapacity } from "./capacity.js";
import { renderGithub } from "./github.js";
import { renderKeys } from "./keys.js";
import { renderNewAgent } from "./new-agent.js";
import { renderNotFound } from "./not-found.js";
import { renderSettings } from "./settings.js";
import { renderWorkflow, renderWorkflows } from "./workflows.js";

export const VIEWS = {
  agents: renderAgents,
  "agent-new": renderNewAgent,
  agent: renderAgent,
  workflows: renderWorkflows,
  workflow: renderWorkflow,
  artifacts: renderArtifacts,
  artifact: renderArtifact,
  capacity: renderCapacity,
  accounts: renderAccounts,
  keys: renderKeys,
  github: renderGithub,
  settings: renderSettings,
  "not-found": renderNotFound,
};

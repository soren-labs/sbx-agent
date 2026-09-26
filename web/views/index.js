import { renderAccounts } from "./accounts.js";
import { renderAgent } from "./agent.js";
import { renderAgents } from "./agents.js";
import { renderArtifact, renderArtifacts } from "./artifacts.js";
import { renderCapacity } from "./capacity.js";
import { renderGithub } from "./github.js";
import { renderHome } from "./home.js";
import { renderIntegrations } from "./integrations.js";
import { renderKeys } from "./keys.js";
import { renderNewAgent } from "./new-agent.js";
import { renderNewTask } from "./new-task.js";
import { renderNotFound } from "./not-found.js";
import { renderRuntime } from "./runtime.js";
import { renderSettings } from "./settings.js";
import { renderTask } from "./task.js";
import { renderTasks } from "./tasks.js";
import { renderWorkflow, renderWorkflows } from "./workflows.js";

export const VIEWS = {
  home: renderHome,
  tasks: renderTasks,
  "task-new": renderNewTask,
  task: renderTask,
  integrations: renderIntegrations,
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
  runtime: renderRuntime,
  settings: renderSettings,
  "not-found": renderNotFound,
};

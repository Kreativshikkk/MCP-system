"use strict";

const state = {
  environments: [],
  selectedEnvironmentId: null,
  operations: [],
  truncated: false,
  repositories: [],
  selectedRepositoryId: null,
  artifactType: "tickets",
  selectedArtifactId: null,
};

const elements = {
  environmentList: document.querySelector("#environment-list"),
  environmentCount: document.querySelector("#environment-count"),
  emptyState: document.querySelector("#empty-state"),
  inspectorContent: document.querySelector("#inspector-content"),
  errorBanner: document.querySelector("#error-banner"),
  refreshButton: document.querySelector("#refresh-button"),
  search: document.querySelector("#search-filter"),
  transport: document.querySelector("#transport-filter"),
  status: document.querySelector("#status-filter"),
  actor: document.querySelector("#actor-filter"),
  timeline: document.querySelector("#timeline-list"),
  timelineEmpty: document.querySelector("#timeline-empty"),
  resultCount: document.querySelector("#result-count"),
  truncatedNote: document.querySelector("#truncated-note"),
  drawer: document.querySelector("#operation-drawer"),
  drawerBackdrop: document.querySelector("#drawer-backdrop"),
  drawerClose: document.querySelector("#drawer-close"),
  repositorySelect: document.querySelector("#repository-select"),
  workbenchEmpty: document.querySelector("#workbench-empty"),
  workbenchContent: document.querySelector("#workbench-content"),
  artifactList: document.querySelector("#artifact-list"),
  artifactDetail: document.querySelector("#artifact-detail"),
};

async function fetchJSON(url) {
  const response = await fetch(url, {headers: {Accept: "application/json"}});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || `Request failed (${response.status})`);
  return payload;
}

async function initialize() {
  setError(null);
  try {
    const payload = await fetchJSON("/api/environments");
    state.environments = payload.environments;
    elements.environmentCount.textContent = String(state.environments.length);
    if (!state.selectedEnvironmentId && state.environments.length) {
      state.selectedEnvironmentId = state.environments[0].id;
    }
    if (
      state.selectedEnvironmentId &&
      !state.environments.some((item) => item.id === state.selectedEnvironmentId)
    ) {
      state.selectedEnvironmentId = state.environments[0]?.id || null;
    }
    renderEnvironments();
    elements.emptyState.hidden = state.environments.length !== 0;
    elements.inspectorContent.hidden = state.environments.length === 0;
    if (state.selectedEnvironmentId) {
      await Promise.all([loadOperations(), loadWorkbench()]);
    }
  } catch (error) {
    setError(error.message);
  }
}

async function loadOperations() {
  setError(null);
  elements.refreshButton.disabled = true;
  try {
    const id = encodeURIComponent(state.selectedEnvironmentId);
    const payload = await fetchJSON(
      `/api/environments/${id}/operations?limit=500&latest=true`,
    );
    state.operations = payload.operations;
    state.truncated = payload.truncated;
    renderEnvironment(payload.environment);
    rebuildActorFilter();
    renderTimeline();
  } catch (error) {
    setError(error.message);
  } finally {
    elements.refreshButton.disabled = false;
  }
}

async function loadWorkbench() {
  try {
    const id = encodeURIComponent(state.selectedEnvironmentId);
    const payload = await fetchJSON(`/api/environments/${id}/workbench`);
    state.repositories = payload.services.flatMap((service) =>
      service.projection.repositories.map((repository) => ({
        ...repository,
        providerId: service.projection.provider.id,
        providerName: service.projection.provider.name,
        providerRepositoryId: repository.id,
        id: `${service.instanceId}:${repository.id}`,
        serviceInstanceId: service.instanceId,
        pluginId: service.pluginId,
      }))
    );
    if (!state.repositories.some((item) => item.id === state.selectedRepositoryId)) {
      const mostActive = state.repositories.slice().sort(
        (left, right) => artifactCount(right) - artifactCount(left)
      )[0];
      state.selectedRepositoryId = mostActive?.id || null;
      state.selectedArtifactId = null;
    }
    renderWorkbench();
  } catch (error) {
    setError(error.message);
  }
}

function renderEnvironments() {
  elements.environmentList.replaceChildren();
  for (const environment of state.environments) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "environment-item";
    button.dataset.environmentId = environment.id;
    button.dataset.testid = `environment-${environment.id}`;
    if (environment.id === state.selectedEnvironmentId) button.classList.add("active");

    const name = document.createElement("strong");
    name.textContent = environment.name;
    const meta = document.createElement("small");
    const dot = document.createElement("span");
    dot.className = `environment-dot ${environment.status === "failed" ? "failed" : ""}`;
    const status = document.createElement("span");
    status.textContent = `${environment.status} · ${shortId(environment.id)}`;
    meta.append(dot, status);
    button.append(name, meta);
    button.addEventListener("click", async () => {
      state.selectedEnvironmentId = environment.id;
      renderEnvironments();
      closeDrawer();
      await Promise.all([loadOperations(), loadWorkbench()]);
    });
    elements.environmentList.append(button);
  }
}

function renderEnvironment(environment) {
  document.querySelector("#environment-name").textContent = environment.name;
  document.querySelector("#environment-id").textContent = environment.id;
  document.querySelector("#environment-template").textContent = environment.snapshotId
    ? `Snapshot · ${environment.snapshotId}`
    : environment.templateId
      ? `Template · ${environment.templateId}`
      : "Standalone environment";
  document.querySelector("#environment-updated").textContent = formatDateTime(environment.updatedAt);
  const status = document.querySelector("#environment-status");
  status.textContent = environment.status;
  status.className = `status-pill ${environment.status === "failed" ? "failed" : ""}`;

  const succeeded = state.operations.filter((item) => item.status === "succeeded").length;
  const failed = state.operations.filter((item) => ["failed", "interrupted"].includes(item.status)).length;
  const actors = new Set(state.operations.map((item) => item.actor)).size;
  const successRate = state.operations.length ? Math.round((succeeded / state.operations.length) * 100) : 0;
  document.querySelector("#stat-total").textContent = String(state.operations.length);
  document.querySelector("#stat-success").textContent = String(succeeded);
  document.querySelector("#stat-failed").textContent = String(failed);
  document.querySelector("#stat-actors").textContent = String(actors);
  document.querySelector("#stat-success-rate").textContent = `${successRate}% success rate`;
}

function rebuildActorFilter() {
  const selected = elements.actor.value;
  const actors = [...new Set(state.operations.map((item) => item.actor))].sort();
  elements.actor.replaceChildren(new Option("All actors", ""));
  for (const actor of actors) elements.actor.append(new Option(actor, actor));
  elements.actor.value = actors.includes(selected) ? selected : "";
}

function renderWorkbench() {
  elements.workbenchEmpty.hidden = state.repositories.length !== 0;
  elements.workbenchContent.hidden = state.repositories.length === 0;
  elements.repositorySelect.replaceChildren();
  for (const repository of state.repositories) {
    const option = new Option(repositoryLabel(repository), repository.id);
    elements.repositorySelect.append(option);
  }
  elements.repositorySelect.value = state.selectedRepositoryId || "";
  const repository = selectedRepository();
  if (!repository) return;
  document.querySelector("#artifact-ticket-count").textContent = String(repository.tickets.length);
  document.querySelector("#artifact-change-count").textContent = String(repository.changeSets.length);
  document.querySelector("#artifact-build-count").textContent = String(repository.builds.length);
  document.querySelector("#artifact-visibility").textContent =
    `${repository.providerName} · ${repository.serviceInstanceId}`;
  renderArtifactList();
}

function selectedRepository() {
  return state.repositories.find((item) => item.id === state.selectedRepositoryId) || null;
}

function artifactCount(repository) {
  return repository.tickets.length + repository.changeSets.length + repository.builds.length;
}

function repositoryLabel(repository) {
  const kind = repository.providerId === "jira" ? "tracker project" : "repository";
  return `${repository.providerName} · ${repository.fullName} · ${kind} · ${repository.serviceInstanceId}`;
}

function selectedProviderLabel() {
  const repository = selectedRepository();
  return repository
    ? `${repository.providerName} · ${repository.serviceInstanceId}`
    : "Unknown provider";
}

function renderArtifactList() {
  const repository = selectedRepository();
  if (!repository) return;
  const artifacts = repository[state.artifactType];
  if (!artifacts.some((item) => item.id === state.selectedArtifactId)) {
    state.selectedArtifactId = artifacts[0]?.id || null;
  }
  elements.artifactList.replaceChildren();
  if (!artifacts.length) {
    const empty = document.createElement("div");
    empty.className = "empty-artifact-list";
    const scope = repository.providerId === "jira" ? "tracker project" : "repository";
    empty.textContent = state.artifactType === "tickets" ? `No tickets in this ${scope}.` : `No change sets in this ${scope}.`;
    elements.artifactList.append(empty);
    renderArtifactDetail(null);
    return;
  }
  for (const artifact of artifacts) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "artifact-list-item";
    button.dataset.artifactId = artifact.id;
    if (artifact.id === state.selectedArtifactId) button.classList.add("active");
    const meta = document.createElement("small");
    appendText(meta, `#${artifact.number}`);
    const status = document.createElement("span");
    const stateName = artifact.merged ? "merged" : artifact.state;
    status.className = `artifact-state ${stateName}`;
    status.textContent = stateName;
    meta.append(status);
    const title = document.createElement("strong");
    title.textContent = artifact.title;
    const author = document.createElement("p");
    author.textContent = `${artifact.author} · ${formatDateTime(artifact.updatedAt)}`;
    button.append(meta, title, author);
    button.addEventListener("click", () => {
      state.selectedArtifactId = artifact.id;
      renderArtifactList();
    });
    elements.artifactList.append(button);
  }
  renderArtifactDetail(artifacts.find((item) => item.id === state.selectedArtifactId));
}

function renderArtifactDetail(artifact) {
  elements.artifactDetail.replaceChildren();
  if (!artifact) {
    const placeholder = document.createElement("div");
    placeholder.className = "artifact-placeholder";
    const icon = document.createElement("span");
    icon.textContent = "↗";
    const title = document.createElement("h3");
    title.textContent = "No artifact selected";
    const text = document.createElement("p");
    text.textContent = "Choose an item from the list to inspect it.";
    placeholder.append(icon, title, text);
    elements.artifactDetail.append(placeholder);
    return;
  }
  if (state.artifactType === "tickets") renderTicketDetail(artifact);
  else renderChangeSetDetail(artifact);
}

function renderTicketDetail(ticket) {
  const header = artifactHeader(`${selectedProviderLabel()} · #${ticket.number} · Ticket`, ticket.title, ticket.description);
  const facts = header.querySelector(".artifact-facts");
  addFact(facts, ticket.state);
  addFact(facts, `author · ${ticket.author}`);
  for (const label of ticket.labels) addFact(facts, label);
  for (const assignee of ticket.assignees) addFact(facts, `assigned · ${assignee}`);
  elements.artifactDetail.append(header);
  const iterations = artifactSection(`Iterations · ${(ticket.iterations || []).length}`);
  if (!(ticket.iterations || []).length) appendEmptyText(iterations, "No iteration assigned.");
  for (const iteration of ticket.iterations || []) {
    const card = document.createElement("article");
    card.className = "iteration-card";
    card.append(cardHeader(iteration.name, iteration.state));
    if (iteration.goal) {
      const goal = document.createElement("p");
      goal.textContent = iteration.goal;
      card.append(goal);
    }
    iterations.append(card);
  }
  elements.artifactDetail.append(iterations);
  const links = artifactSection(`Issue links · ${(ticket.links || []).length}`);
  if (!(ticket.links || []).length) appendEmptyText(links, "No linked tickets.");
  for (const link of ticket.links || []) {
    const card = document.createElement("article");
    card.className = "link-card";
    card.append(cardHeader(link.issueKey, `${link.type} · ${link.direction}`));
    links.append(card);
  }
  elements.artifactDetail.append(links);
  const comments = artifactSection(`Comments · ${ticket.comments.length}`);
  if (!ticket.comments.length) appendEmptyText(comments, "No comments yet.");
  for (const comment of ticket.comments) {
    const card = document.createElement("article");
    card.className = "comment-card";
    card.append(cardHeader(comment.author, formatDateTime(comment.createdAt)));
    const body = document.createElement("p");
    body.textContent = comment.body;
    card.append(body);
    comments.append(card);
  }
  elements.artifactDetail.append(comments);
}

function renderChangeSetDetail(changeSet) {
  const header = artifactHeader(`${selectedProviderLabel()} · #${changeSet.number} · Change set`, changeSet.title, changeSet.description);
  const facts = header.querySelector(".artifact-facts");
  addFact(facts, changeSet.merged ? "merged" : changeSet.state);
  addFact(facts, `author · ${changeSet.author}`);
  addFact(facts, `${changeSet.head.ref} → ${changeSet.base.ref}`);
  addFact(facts, changeSet.draft ? "draft" : changeSet.mergeableState);
  elements.artifactDetail.append(header);

  const repository = selectedRepository();
  const builds = repository.builds.filter((build) => build.headSha === changeSet.head.sha);
  const buildSection = artifactSection(`Builds · ${builds.length}`);
  if (!builds.length) appendEmptyText(buildSection, "No builds recorded for this head commit.");
  for (const build of builds) {
    const card = document.createElement("article");
    card.className = "build-card";
    card.append(cardHeader(`${build.name} · #${build.runNumber}`, build.conclusion || build.status));
    buildSection.append(card);
  }
  elements.artifactDetail.append(buildSection);

  const reviews = artifactSection(`Reviews · ${changeSet.reviews.length}`);
  if (!changeSet.reviews.length) appendEmptyText(reviews, "No reviews submitted.");
  for (const review of changeSet.reviews) {
    const card = document.createElement("article");
    card.className = "review-card";
    card.append(cardHeader(review.reviewer, review.state));
    if (review.body) {
      const body = document.createElement("p");
      body.textContent = review.body;
      card.append(body);
    }
    reviews.append(card);
  }
  elements.artifactDetail.append(reviews);

  const diffSection = artifactSection("Change diff");
  const diffMeta = document.createElement("div");
  diffMeta.className = "diff-meta";
  appendText(diffMeta, shortSha(changeSet.base.sha));
  appendText(diffMeta, `→ ${shortSha(changeSet.head.sha)}`);
  diffSection.append(diffMeta);
  const patch = document.createElement("pre");
  patch.className = "diff-view";
  patch.textContent = changeSet.diff.available
    ? (changeSet.diff.patch || "No textual changes between these commits.")
    : "Diff is unavailable for this change set.";
  diffSection.append(patch);
  elements.artifactDetail.append(diffSection);
}

function artifactHeader(kicker, titleText, description) {
  const header = document.createElement("header");
  header.className = "artifact-detail-header";
  const small = document.createElement("small");
  small.textContent = kicker;
  const title = document.createElement("h2");
  title.textContent = titleText;
  const body = document.createElement("p");
  body.textContent = description || "No description provided.";
  const facts = document.createElement("div");
  facts.className = "artifact-facts";
  header.append(small, title, body, facts);
  return header;
}

function artifactSection(titleText) {
  const section = document.createElement("section");
  section.className = "artifact-section";
  const title = document.createElement("h3");
  title.textContent = titleText;
  section.append(title);
  return section;
}

function cardHeader(left, right) {
  const header = document.createElement("header");
  const strong = document.createElement("strong");
  strong.textContent = left;
  const status = document.createElement("span");
  status.textContent = right;
  header.append(strong, status);
  return header;
}

function addFact(container, value) {
  if (!value) return;
  const fact = document.createElement("span");
  fact.textContent = value;
  container.append(fact);
}

function appendEmptyText(container, value) {
  const text = document.createElement("p");
  text.className = "empty-artifact-list";
  text.textContent = value;
  container.append(text);
}

function activateView(view) {
  for (const item of document.querySelectorAll(".view-tab")) {
    item.classList.toggle("active", item.dataset.view === view);
  }
  const workbench = view === "workbench";
  document.querySelector("#activity-view").hidden = workbench;
  document.querySelector("#workbench-view").hidden = !workbench;
}

function activateArtifactType(type) {
  state.artifactType = type;
  state.selectedArtifactId = null;
  for (const item of document.querySelectorAll(".artifact-tab")) {
    item.classList.toggle("active", item.dataset.artifactType === type);
  }
  if (state.repositories.length) renderArtifactList();
}

function filteredOperations() {
  const query = elements.search.value.trim().toLocaleLowerCase();
  return state.operations.filter((item) => {
    if (elements.transport.value && item.transport !== elements.transport.value) return false;
    if (elements.status.value && item.status !== elements.status.value) return false;
    if (elements.actor.value && item.actor !== elements.actor.value) return false;
    if (!query) return true;
    const searchable = [
      item.operation,
      item.actor,
      item.transport,
      item.pluginId,
      item.serviceInstanceId,
      JSON.stringify(item.request),
      JSON.stringify(item.error),
    ].join(" ").toLocaleLowerCase();
    return searchable.includes(query);
  });
}

function renderTimeline() {
  const operations = filteredOperations().slice().reverse();
  elements.timeline.replaceChildren();
  elements.timelineEmpty.hidden = operations.length !== 0;
  elements.timeline.hidden = operations.length === 0;
  elements.resultCount.textContent = `${operations.length} ${operations.length === 1 ? "operation" : "operations"}`;
  elements.truncatedNote.hidden = !state.truncated;

  for (const operation of operations) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "operation-card";
    card.dataset.operationId = operation.id;
    card.dataset.testid = `operation-${operation.id}`;

    const icon = document.createElement("span");
    icon.className = `status-icon ${operation.status}`;
    icon.textContent = operation.status === "succeeded" ? "✓" : operation.status === "failed" ? "!" : "…";

    const title = document.createElement("span");
    title.className = "operation-title";
    const name = document.createElement("strong");
    name.textContent = operation.operation;
    const meta = document.createElement("small");
    appendText(meta, operation.actor);
    appendSeparator(meta);
    const transport = document.createElement("span");
    transport.className = `transport-tag ${operation.transport}`;
    transport.textContent = operation.transport;
    meta.append(transport);
    appendSeparator(meta);
    appendText(meta, operation.serviceInstanceId);
    title.append(name, meta);

    const time = document.createElement("span");
    time.className = "operation-time";
    const timestamp = document.createElement("strong");
    timestamp.textContent = formatDateTime(operation.startedAt);
    const duration = document.createElement("small");
    duration.textContent = formatDuration(operation.startedAt, operation.completedAt);
    time.append(timestamp, duration);

    card.append(icon, title, time);
    card.addEventListener("click", () => openDrawer(operation));
    elements.timeline.append(card);
  }
}

function openDrawer(operation) {
  document.querySelector("#drawer-operation").textContent = operation.operation;
  const meta = document.querySelector("#drawer-meta");
  meta.replaceChildren();
  for (const value of [
    operation.status,
    operation.actor,
    operation.transport.toUpperCase(),
    operation.serviceInstanceId,
    shortId(operation.id),
    formatDateTime(operation.startedAt),
  ]) {
    const badge = document.createElement("span");
    badge.textContent = value;
    meta.append(badge);
  }
  document.querySelector("#drawer-request").textContent = prettyJSON(operation.request);
  document.querySelector("#drawer-result").textContent = prettyJSON(operation.result);
  document.querySelector("#drawer-error").textContent = prettyJSON(operation.error);
  document.querySelector("#drawer-result-section").hidden = operation.result === null;
  document.querySelector("#drawer-error-section").hidden = operation.error === null;
  elements.drawer.hidden = false;
  elements.drawerBackdrop.hidden = false;
  document.body.style.overflow = "hidden";
  elements.drawerClose.focus();
}

function closeDrawer() {
  elements.drawer.hidden = true;
  elements.drawerBackdrop.hidden = true;
  document.body.style.overflow = "";
}

function setError(message) {
  elements.errorBanner.hidden = !message;
  elements.errorBanner.textContent = message || "";
}

function appendText(parent, value) {
  const span = document.createElement("span");
  span.textContent = value;
  parent.append(span);
}

function appendSeparator(parent) {
  const separator = document.createElement("span");
  separator.className = "meta-separator";
  separator.textContent = "·";
  parent.append(separator);
}

function shortId(value) {
  return value.length > 12 ? `${value.slice(0, 8)}…` : value;
}

function shortSha(value) {
  return value ? value.slice(0, 8) : "unknown";
}

function prettyJSON(value) {
  return JSON.stringify(value, null, 2);
}

function formatDateTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function formatDuration(startedAt, completedAt) {
  if (!completedAt) return "in progress";
  const milliseconds = Math.max(0, new Date(completedAt) - new Date(startedAt));
  if (milliseconds < 1000) return `${milliseconds} ms`;
  return `${(milliseconds / 1000).toFixed(2)} s`;
}

for (const filter of [elements.search, elements.transport, elements.status, elements.actor]) {
  filter.addEventListener("input", renderTimeline);
  filter.addEventListener("change", renderTimeline);
}
elements.refreshButton.addEventListener("click", initialize);
elements.drawerClose.addEventListener("click", closeDrawer);
elements.drawerBackdrop.addEventListener("click", closeDrawer);
elements.repositorySelect.addEventListener("change", () => {
  state.selectedRepositoryId = elements.repositorySelect.value;
  state.selectedArtifactId = null;
  renderWorkbench();
});
for (const tab of document.querySelectorAll(".view-tab")) {
  tab.addEventListener("click", () => {
    activateView(tab.dataset.view);
    history.replaceState(null, "", tab.dataset.view === "workbench" ? "#artifacts" : location.pathname);
  });
}
for (const tab of document.querySelectorAll(".artifact-tab")) {
  tab.addEventListener("click", () => {
    activateArtifactType(tab.dataset.artifactType);
    history.replaceState(
      null,
      "",
      tab.dataset.artifactType === "changeSets" ? "#artifacts/changesets" : "#artifacts"
    );
  });
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !elements.drawer.hidden) closeDrawer();
});

if (location.hash.startsWith("#artifacts")) activateView("workbench");
if (location.hash === "#artifacts/changesets") activateArtifactType("changeSets");
initialize();

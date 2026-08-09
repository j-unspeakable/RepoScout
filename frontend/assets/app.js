const APPLICATION_BASE_URL = new URL("./", document.baseURI);
const TOP_K = 5;

class ApiError extends Error {
  constructor(status, detail, retryAfter = null) {
    super("RepoScout request failed");
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
  }
}

function apiUrl(relativePath) {
  if (
    typeof relativePath !== "string" ||
    relativePath.length === 0 ||
    relativePath.startsWith("/") ||
    relativePath.startsWith("//") ||
    /^[a-z][a-z\d+.-]*:/i.test(relativePath)
  ) {
    throw new TypeError("API paths must be relative to the application base");
  }
  return new URL(relativePath, APPLICATION_BASE_URL);
}

async function apiRequest(relativePath, options = {}) {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(apiUrl(relativePath), { ...options, headers });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    if (!response.ok) {
      throw new ApiError(response.status, null, response.headers.get("Retry-After"));
    }
  }

  if (!response.ok) {
    throw new ApiError(
      response.status,
      payload?.detail ?? null,
      response.headers.get("Retry-After"),
    );
  }
  return payload;
}

function element(tagName, className = "", text = "") {
  const node = document.createElement(tagName);
  if (className) {
    node.className = className;
  }
  if (text) {
    node.textContent = text;
  }
  return node;
}

function formatCount(value) {
  const count = Number(value);
  return Number.isFinite(count) && count >= 0 ? new Intl.NumberFormat().format(count) : "—";
}

function formatIndexedTime(value) {
  if (!value) {
    return "Not indexed yet";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Index time unavailable";
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function setMetricValue(node, value) {
  node.textContent = formatCount(value);
  node.classList.remove("metric-loading");
}

async function loadCorpusSummary() {
  const metrics = document.querySelector("#corpus-metrics");
  const detail = document.querySelector("#corpus-detail");
  const retry = document.querySelector("#corpus-retry");
  metrics.setAttribute("aria-busy", "true");
  retry.hidden = true;

  try {
    const summary = await apiRequest("corpus/summary");
    setMetricValue(document.querySelector("#metric-repositories"), summary.repositories_ingested);
    setMetricValue(document.querySelector("#metric-searchable"), summary.repositories_searchable);
    setMetricValue(document.querySelector("#metric-chunks"), summary.searchable_chunks);
    detail.textContent = `${formatCount(summary.readmes_available)} repositories have README content available · Last indexed ${formatIndexedTime(summary.last_indexed_at)}`;
  } catch {
    detail.textContent = "Search readiness is temporarily unavailable. Search is still available.";
    retry.hidden = false;
  } finally {
    metrics.setAttribute("aria-busy", "false");
  }
}

function activeViewFromHash() {
  return window.location.hash === "#ask" ? "ask" : "discover";
}

function showView(viewName, { moveFocus = false } = {}) {
  const title = document.querySelector("#mode-title");
  title.textContent = viewName === "ask" ? "Ask RepoScout" : "Discover Projects";

  for (const view of document.querySelectorAll("[data-view]")) {
    view.hidden = view.dataset.view !== viewName;
  }
  for (const link of document.querySelectorAll("[data-nav-view]")) {
    if (link.dataset.navView === viewName) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  }
  if (moveFocus) {
    title.setAttribute("tabindex", "-1");
    title.focus({ preventScroll: true });
  }
}

function normalizeInitialHash() {
  if (window.location.hash !== "#discover" && window.location.hash !== "#ask") {
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#discover`);
  }
  showView(activeViewFromHash());
}

function setupExampleQueries() {
  for (const button of document.querySelectorAll("[data-query-target]")) {
    const query = document.querySelector(`#${button.dataset.queryTarget}`);
    if (!(query instanceof HTMLTextAreaElement)) {
      continue;
    }
    button.addEventListener("click", () => {
      query.value = button.textContent.trim();
      query.setAttribute("aria-invalid", "false");
      query.focus();
    });
  }
}

function buildSearchPayload(form) {
  const data = new FormData(form);
  const query = String(data.get("query") ?? "").trim();
  const language = String(data.get("language") ?? "").trim();
  const rawStars = String(data.get("minimum_stars") ?? "").trim();
  const filters = {};

  if (language) {
    filters.language = language;
  }
  if (rawStars) {
    filters.minimum_stars = Number(rawStars);
  }

  return {
    query,
    top_k: TOP_K,
    ...(Object.keys(filters).length > 0 ? { filters } : {}),
  };
}

function validateSearchForm(form, payload) {
  const query = form.querySelector("textarea[name='query']");
  const stars = form.querySelector("input[name='minimum_stars']");
  query.setAttribute("aria-invalid", "false");
  stars.setAttribute("aria-invalid", "false");

  if (!payload.query) {
    query.setAttribute("aria-invalid", "true");
    query.focus();
    return "Describe what you want RepoScout to find.";
  }
  if (payload.query.length > 500) {
    query.setAttribute("aria-invalid", "true");
    query.focus();
    return "Keep your request to 500 characters or fewer.";
  }
  if (
    payload.filters?.minimum_stars !== undefined &&
    (!Number.isInteger(payload.filters.minimum_stars) || payload.filters.minimum_stars < 0)
  ) {
    stars.setAttribute("aria-invalid", "true");
    stars.focus();
    return "Minimum stars must be a nonnegative whole number.";
  }
  return null;
}

function setStatus(node, message = "", kind = "") {
  node.textContent = message;
  node.className = "request-status";
  if (kind) {
    node.classList.add(`is-${kind}`);
  }
}

function createLoadingPanel() {
  const panel = element("div", "loading-panel");
  panel.setAttribute("aria-hidden", "true");
  for (let index = 0; index < 4; index += 1) {
    panel.append(element("span", "skeleton-line"));
  }
  return panel;
}

function showLoading(results, count = 2) {
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < count; index += 1) {
    fragment.append(createLoadingPanel());
  }
  results.replaceChildren(fragment);
  results.setAttribute("aria-busy", "true");
}

function addCoverageAction(container, searchQuery) {
  const action = element("button", "coverage-action-button", "Request more coverage");
  action.type = "button";
  action.addEventListener("click", () => openCoveragePanel(searchQuery));
  container.append(action);
}

function showEmpty(results, title, message, coverageQuery = "") {
  const panel = element("div", "empty-panel");
  panel.append(element("h3", "", title), element("p", "", message));
  addCoverageAction(panel, coverageQuery);
  results.replaceChildren(panel);
}

function friendlyError(error) {
  if (!(error instanceof ApiError)) {
    return "RepoScout could not reach the service. Check your connection and try again.";
  }
  if (error.status === 422) {
    return "Check your request and filters, then try again.";
  }
  if (error.status === 502) {
    return "RepoScout could not complete the generated answer. Please try again.";
  }
  if (error.status === 503) {
    const retry = /^\d+$/.test(error.retryAfter ?? "") ? ` Try again in ${error.retryAfter} seconds.` : "";
    return `RepoScout is temporarily unavailable.${retry}`;
  }
  if (error.status === 504) {
    return "The answer took too long to generate. Please try again.";
  }
  return "RepoScout could not complete that request. Please try again.";
}

function safeGitHubUrl(value) {
  if (typeof value !== "string") {
    return null;
  }
  try {
    const url = new URL(value);
    if (url.protocol === "https:" && url.hostname.toLowerCase() === "github.com") {
      return url;
    }
  } catch {
    return null;
  }
  return null;
}

function metadataChip(text) {
  return element("span", "metadata-chip", text);
}

function evidenceDomId(project, chunk) {
  return `evidence-${project.repo_id}-${chunk.chunk_index}`;
}

function createProjectCard(project, citationTargets) {
  const card = element("article", "project-card");
  card.dataset.repoId = String(project.repo_id ?? "");

  const header = element("div", "project-header");
  const titleWrap = element("div", "project-title-wrap");
  titleWrap.append(
    element("span", "rank-badge", `#${project.rank ?? "—"}`),
    element("h3", "", project.full_name || project.name || "Unnamed repository"),
  );
  header.append(titleWrap);

  const githubUrl = safeGitHubUrl(project.html_url);
  if (githubUrl) {
    const link = element("a", "github-link", "View on GitHub ↗");
    link.href = githubUrl.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    header.append(link);
  }
  card.append(header);

  card.append(
    element(
      "p",
      "project-description",
      project.description || "No project description is currently available.",
    ),
  );

  const metadata = element("div", "metadata-row");
  if (project.primary_language) {
    metadata.append(metadataChip(project.primary_language));
  }
  metadata.append(metadataChip(`★ ${formatCount(project.stars)} stars`));
  metadata.append(metadataChip(`${formatCount(project.forks)} forks`));
  if (project.license) {
    metadata.append(metadataChip(project.license));
  }
  card.append(metadata);

  if (Array.isArray(project.topics) && project.topics.length > 0) {
    const topics = element("div", "topic-list");
    for (const topic of project.topics.slice(0, 8)) {
      topics.append(element("span", "topic-chip", topic));
    }
    card.append(topics);
  }

  card.append(element("div", "project-action-slot"));

  if (Array.isArray(project.evidence) && project.evidence.length > 0) {
    const details = element("details", "evidence-group");
    details.append(element("summary", "", "Why this matched"));
    const list = element("div", "evidence-list");

    for (const chunk of project.evidence) {
      const evidence = element("div", "evidence-chunk", chunk.chunk_text || "Evidence unavailable.");
      evidence.id = evidenceDomId(project, chunk);
      evidence.setAttribute("tabindex", "-1");
      list.append(evidence);

      const citation = `[${project.full_name}#chunk-${chunk.chunk_index}]`;
      citationTargets.set(citation, { details, evidence });
    }
    details.append(list);
    card.append(details);
  }

  return card;
}

function appendAnswerInline(parent, text, citationTargets) {
  const pattern = /(\[[^\]\n\s]+\/[^\]\n\s]+#chunk-\d+\]|\*\*[^*\n]+\*\*)/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > cursor) {
      parent.append(document.createTextNode(text.slice(cursor, match.index)));
    }
    const token = match[0];
    if (token.startsWith("**") && token.endsWith("**")) {
      parent.append(element("strong", "", token.slice(2, -2)));
    } else if (citationTargets.has(token)) {
      const button = element("button", "citation-button", token);
      button.type = "button";
      button.setAttribute("aria-label", `Open evidence ${token}`);
      button.addEventListener("click", () => {
        const target = citationTargets.get(token);
        target.details.open = true;
        target.evidence.scrollIntoView({ behavior: "smooth", block: "center" });
        target.evidence.focus({ preventScroll: true });
      });
      parent.append(button);
    } else {
      parent.append(document.createTextNode(token));
    }
    cursor = match.index + token.length;
  }
  if (cursor < text.length) {
    parent.append(document.createTextNode(text.slice(cursor)));
  }
}

function renderAnswerBody(container, answer, citationTargets) {
  const lines = String(answer ?? "").split(/\r?\n/);
  let list = null;
  let listType = null;

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      list = null;
      listType = null;
      continue;
    }

    const bullet = line.match(/^[-*]\s+(.+)/);
    const numbered = line.match(/^\d+[.)]\s+(.+)/);
    if (bullet || numbered) {
      const nextType = bullet ? "ul" : "ol";
      if (list === null || listType !== nextType) {
        list = element(nextType);
        listType = nextType;
        container.append(list);
      }
      const item = element("li");
      appendAnswerInline(item, (bullet ?? numbered)[1], citationTargets);
      list.append(item);
      continue;
    }

    list = null;
    listType = null;
    const heading = line.match(/^#{1,4}\s+(.+)/);
    const paragraph = element(heading ? "h4" : "p");
    appendAnswerInline(paragraph, heading ? heading[1] : line, citationTargets);
    container.append(paragraph);
  }
}

function renderProjects(
  projects,
  results,
  { emptyTitle, emptyMessage, coverageQuery = "" } = {},
) {
  if (!Array.isArray(projects) || projects.length === 0) {
    showEmpty(results, emptyTitle, emptyMessage, coverageQuery);
    return;
  }
  const targets = new Map();
  const fragment = document.createDocumentFragment();
  for (const project of projects) {
    fragment.append(createProjectCard(project, targets));
  }
  results.replaceChildren(fragment);
}

function renderAskResponse(payload, results) {
  const projects = Array.isArray(payload.projects) ? payload.projects : [];
  const citationTargets = new Map();
  const projectFragment = document.createDocumentFragment();
  for (const project of projects) {
    projectFragment.append(createProjectCard(project, citationTargets));
  }

  const fragment = document.createDocumentFragment();
  const answer = element("article", "answer-card");
  const header = element("div", "answer-header");
  header.append(element("h3", "", "RepoScout’s answer"), element("span", "grounded-badge", "Grounded"));
  const body = element("div", "answer-body");
  renderAnswerBody(body, payload.answer || "RepoScout could not produce an answer.", citationTargets);
  answer.append(header, body);
  fragment.append(answer);

  if (projects.length > 0) {
    fragment.append(element("p", "recommendation-heading", "Recommended projects"), projectFragment);
  } else {
    const empty = element("div", "empty-panel");
    empty.append(
      element("h3", "", "No supporting projects found"),
      element("p", "", "Try broader wording or remove one of the optional filters."),
    );
    addCoverageAction(empty, payload.query || "");
    fragment.append(empty);
  }
  results.replaceChildren(fragment);
}

function activeSearchQuery() {
  const queryId = activeViewFromHash() === "ask" ? "#ask-query" : "#discover-query";
  return document.querySelector(queryId).value.trim();
}

function clearIndexingRequestStatus() {
  const status = document.querySelector("#indexing-request-status");
  delete status.dataset.completed;
  setStatus(status, "");
}

function openCoveragePanel(prefill = "") {
  const panel = document.querySelector("#coverage-panel");
  const toggle = document.querySelector("#coverage-toggle");
  const query = document.querySelector("#indexing-search-query");
  const status = document.querySelector("#indexing-request-status");

  if (status.dataset.completed === "true") {
    clearIndexingRequestStatus();
  }
  panel.hidden = false;
  toggle.setAttribute("aria-expanded", "true");
  if (!query.value.trim() && prefill.trim()) {
    query.value = prefill.trim();
  }
  query.focus({ preventScroll: true });
}

function dismissCoveragePanel() {
  document.querySelector("#coverage-panel").hidden = true;
  document.querySelector("#coverage-toggle").setAttribute("aria-expanded", "false");
  clearIndexingRequestStatus();
  document.querySelector("#coverage-toggle").focus({ preventScroll: true });
}

function validateIndexingRequest(form) {
  const query = form.querySelector("textarea[name='search_query']");
  const notes = form.querySelector("textarea[name='notes']");
  const searchQuery = query.value.trim();
  const context = notes.value.trim();
  query.setAttribute("aria-invalid", "false");
  notes.setAttribute("aria-invalid", "false");

  if (!searchQuery) {
    query.setAttribute("aria-invalid", "true");
    query.focus();
    return { error: "Describe what you were hoping to find." };
  }
  if (searchQuery.length > 500) {
    query.setAttribute("aria-invalid", "true");
    query.focus();
    return { error: "Keep the requested topic to 500 characters or fewer." };
  }
  if (context.length > 2000) {
    notes.setAttribute("aria-invalid", "true");
    notes.focus();
    return { error: "Keep the additional context to 2,000 characters or fewer." };
  }
  return {
    payload: {
      search_query: searchQuery,
      notes: context || null,
    },
  };
}

function setIndexingRequestLoading(form, loading) {
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = loading;
  submit.classList.toggle("is-loading", loading);
  form.setAttribute("aria-busy", String(loading));
  for (const control of form.querySelectorAll("textarea")) {
    control.disabled = loading;
  }
}

function setupIndexingRequest() {
  const form = document.querySelector("#indexing-request-form");
  const status = document.querySelector("#indexing-request-status");
  const toggle = document.querySelector("#coverage-toggle");
  let submitting = false;

  toggle.addEventListener("click", () => {
    if (document.querySelector("#coverage-panel").hidden) {
      openCoveragePanel(activeSearchQuery());
    } else {
      dismissCoveragePanel();
    }
  });
  document.querySelector("#coverage-dismiss").addEventListener("click", dismissCoveragePanel);
  form.addEventListener("input", () => {
    if (status.dataset.completed === "true") {
      clearIndexingRequestStatus();
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) {
      return;
    }
    if (status.dataset.completed === "true") {
      clearIndexingRequestStatus();
    }

    const validation = validateIndexingRequest(form);
    if (validation.error) {
      setStatus(status, validation.error, "error");
      return;
    }

    submitting = true;
    delete status.dataset.completed;
    setIndexingRequestLoading(form, true);
    setStatus(status, "Submitting your request for review…");
    try {
      await apiRequest("indexing-requests", {
        method: "POST",
        body: JSON.stringify(validation.payload),
      });
      form.reset();
      setStatus(status, "Request received for review.", "success");
      status.dataset.completed = "true";
      status.setAttribute("tabindex", "-1");
      status.focus({ preventScroll: true });
    } catch (error) {
      setStatus(status, friendlyError(error), "error");
    } finally {
      submitting = false;
      setIndexingRequestLoading(form, false);
    }
  });
}

function setRequestLoading(form, loading) {
  const submit = form.querySelector("button[type='submit']");
  const cancel = form.querySelector(".cancel-button");
  submit.disabled = loading;
  submit.classList.toggle("is-loading", loading);
  form.setAttribute("aria-busy", String(loading));
  for (const control of form.querySelectorAll("textarea, input")) {
    control.disabled = loading;
  }
  cancel.hidden = !loading;
}

function setupSearchMode({ formId, statusId, resultsId, endpoint, mode }) {
  const form = document.querySelector(`#${formId}`);
  const status = document.querySelector(`#${statusId}`);
  const results = document.querySelector(`#${resultsId}`);
  const cancel = form.querySelector(".cancel-button");
  const submit = form.querySelector("button[type='submit']");
  let controller = null;

  cancel.addEventListener("click", () => controller?.abort());

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (controller !== null) {
      return;
    }

    const payload = buildSearchPayload(form);
    const validationMessage = validateSearchForm(form, payload);
    if (validationMessage) {
      setStatus(status, validationMessage, "error");
      return;
    }

    controller = new AbortController();
    setRequestLoading(form, true);
    results.setAttribute("aria-busy", "true");
    showLoading(results, mode === "ask" ? 3 : 2);
    setStatus(
      status,
      mode === "ask"
        ? "RepoScout is reviewing the indexed projects. This may take several seconds…"
        : "Searching repositories…",
    );

    try {
      const response = await apiRequest(endpoint, {
        method: "POST",
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      if (mode === "ask") {
        renderAskResponse(response, results);
      } else {
        renderProjects(response.projects, results, {
          emptyTitle: "No matching projects yet",
          emptyMessage: "Try broader wording, another language, or a lower star filter.",
          coverageQuery: payload.query,
        });
      }
      setStatus(status, "");
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        results.replaceChildren();
        setStatus(status, "Request cancelled. Your search is ready to edit or retry.", "cancelled");
      } else {
        results.replaceChildren();
        setStatus(status, friendlyError(error), "error");
      }
    } finally {
      controller = null;
      results.setAttribute("aria-busy", "false");
      setRequestLoading(form, false);
      submit.focus({ preventScroll: true });
    }
  });
}

document.querySelector("#corpus-retry").addEventListener("click", loadCorpusSummary);
window.addEventListener("hashchange", () => showView(activeViewFromHash(), { moveFocus: true }));

normalizeInitialHash();
setupExampleQueries();
setupIndexingRequest();
setupSearchMode({
  formId: "discover-form",
  statusId: "discover-status",
  resultsId: "discover-results",
  endpoint: "search/semantic",
  mode: "discover",
});
setupSearchMode({
  formId: "ask-form",
  statusId: "ask-status",
  resultsId: "ask-results",
  endpoint: "search/ask",
  mode: "ask",
});
loadCorpusSummary();

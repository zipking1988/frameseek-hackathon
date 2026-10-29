/* Deterministic fixture conversation and review routes, never general NLP or a model. */
(function (root) {
  "use strict";
  const normalize = (value) => value.normalize("NFKC").toLowerCase().replace(/[\s？?。！!，,]/g, "");
  function resolve(question, queries, previousId) {
    const value = normalize(question);
    const exact = queries.find((query) => normalize(query.question) === value);
    if (exact) return { query: exact, usedContext: false };
    // These are explicit aliases for the labeled synthetic demonstration, not
    // general video understanding or a keyword-based inference.
    const aliases = {
      包裹何时在中央: "center",
      包裹何时出现在中央: "center",
      whenistheparcelinthecenter: "center",
      whenistheparcelontheright: "right",
      包裹什么时候移到右侧: "right",
      视频中有没有叉车: "forklift",
    };
    const example = queries.find((query) => query.id === aliases[value]);
    if (example) return { query: example, usedContext: false };
    const previous = queries.find((query) => query.id === previousId);
    // One explicit English fixture follow-up, scoped to the preceding center answer.
    if (value === "whathappensnext" && previous?.id === "center") {
      const query = queries.find((item) => item.id === "right") || null;
      return { query, usedContext: Boolean(query) };
    }
    const repeat = ["看看证据", "能看一下证据吗"].includes(value);
    if (previous && repeat) return { query: previous, usedContext: true };
    const match = Object.entries(previous?.followup_map || {}).find(
      ([label]) => normalize(label) === value,
    );
    if (match)
      return {
        query: queries.find((q) => q.id === match[1]) || null,
        usedContext: true,
      };
    const parcelContext = ["center", "right", "person"].includes(previousId);
    const followups = {
      那后来呢: "right",
      后来呢: "right",
      之后呢: "right",
      那之后呢: "right",
      然后呢: "right",
      后来去哪了: "right",
      那是谁搬的: "person",
      谁搬的: "person",
      是谁搬的: "person",
      能确认是谁搬的吗: "person",
    };
    const id = followups[value];
    const query =
      parcelContext && id ? queries.find((item) => item.id === id) : null;
    return { query: query || null, usedContext: Boolean(query) };
  }
  function materialize(scenario, queries) {
    if (
      !scenario ||
      !Array.isArray(scenario.steps) ||
      !scenario.steps.length ||
      scenario.steps.length > 12
    )
      throw new Error("Invalid demo route");
    return scenario.steps.map((step, index) => {
      const query = queries.find((q) => q.id === step.query_id);
      if (
        !query ||
        (step.parent !== null &&
          (!Number.isInteger(step.parent) ||
            step.parent < 0 ||
            step.parent >= index))
      )
        throw new Error("Invalid demo reference");
      return {
        ...query,
        question: step.question || query.question,
        usedContext: step.parent !== null,
        parentIndex: step.parent,
      };
    });
  }
  const api = { resolve, materialize };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FrameSeekDemoConversation = api;
})(typeof window !== "undefined" ? window : globalThis);

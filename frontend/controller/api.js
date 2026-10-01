sap.ui.define([], () => {
  "use strict";
  // GET (no body) or POST JSON; throws an Error with the FastAPI `detail` message on failure
  return async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
      throw new Error(detail || `${response.status} ${response.statusText}`);
    }
    return data;
  };
});

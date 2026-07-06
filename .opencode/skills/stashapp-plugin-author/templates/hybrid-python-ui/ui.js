(() => {
  "use strict";

  const api = window.PluginApi;
  if (!api?.React || !api?.register?.route) {
    console.warn("[__PLUGIN_ID__] compatible PluginApi not available");
    return;
  }

  const React = api.React;
  const Bootstrap = api.libraries?.Bootstrap;
  const Button = Bootstrap?.Button || "button";

  function PluginPage() {
    return React.createElement(
      "main",
      { className: "__PLUGIN_CSS_ID__-page" },
      React.createElement("h2", null, "__PLUGIN_NAME__"),
      React.createElement(
        "p",
        null,
        "This route is registered through Stash's experimental PluginApi."
      ),
      React.createElement(
        Button,
        { type: "button", onClick: () => console.info("[__PLUGIN_ID__] test") },
        "Test"
      )
    );
  }

  try {
    api.register.route("/plugin/__PLUGIN_ID__", PluginPage);
  } catch (error) {
    console.error("[__PLUGIN_ID__] route registration failed", error);
  }
})();

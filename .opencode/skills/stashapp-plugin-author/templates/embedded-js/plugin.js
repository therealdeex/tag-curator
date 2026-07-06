(function () {
  "use strict";

  var args = (input && input.args) || {};
  log.Info("[__PLUGIN_ID__] starting");

  var data = gql.Do(
    `query EmbeddedSceneCount($filter: FindFilterType) {
       findScenes(filter: $filter) { count }
     }`,
    { filter: { per_page: 1 } }
  );

  log.Progress(1.0);
  return {
    output: {
      ok: true,
      mode: args.mode || "manual",
      sceneCount: data.findScenes.count
    }
  };
})();

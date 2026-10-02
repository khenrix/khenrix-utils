// Public synthetic lifecycle example, deliberately flawed.
exports.loadProfile = function(fetcher, publish) {
  const controller = new AbortController();
  fetcher({signal: controller.signal}).then(publish);
  return () => {};
};

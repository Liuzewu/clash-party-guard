// Add subscription nodes to the named selector when Clash Party loads a profile.
// Change the selector name and exclusion pattern to suit your own configuration.
function main(config) {
  const groupName = 'Example Selector';
  const exclude = /测试|測試/i;
  const nodes = (config.proxies || []).map(node => node.name).filter(name => name && !exclude.test(name));
  const groups = config['proxy-groups'] || [];
  const group = groups.find(item => item.name === groupName);
  if (!group || group.type !== 'select') return config;
  const groupsByName = new Set(groups.map(item => item.name));
  const nodeNames = new Set(nodes);
  const existing = (group.proxies || []).filter(name => name === 'DIRECT' || name === 'REJECT' || groupsByName.has(name) || nodeNames.has(name));
  group.proxies = Array.from(new Set(existing.concat(nodes)));
  return config;
}

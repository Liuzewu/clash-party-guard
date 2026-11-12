// Add subscription nodes and optional parent selectors to a Party selector.
// Change the group names and exclusion pattern in main() for your profile.
function configureSelector(config, options) {
  const groupName = options.groupName;
  const groups = Array.isArray(config['proxy-groups']) ? config['proxy-groups'] : [];
  const byName = new Map(groups.filter(group => group && typeof group.name === 'string')
    .map(group => [group.name, group]));
  const group = byName.get(groupName);
  if (!group || group.type !== 'select') return config;
  const nodes = (Array.isArray(config.proxies) ? config.proxies : [])
    .map(node => node && node.name)
    .filter(name => typeof name === 'string' && name && !byName.has(name) && !options.exclude.test(name));
  const nodeNames = new Set(nodes);
  const existing = (Array.isArray(group.proxies) ? group.proxies : [])
    .filter(name => name === 'DIRECT' || name === 'REJECT' || byName.has(name) || nodeNames.has(name));

  // A parent must exist, be manually selectable, and have no path back to
  // this selector (or another group cycle). Never activate it automatically.
  function safeParent(name) {
    const parent = byName.get(name);
    if (!parent || parent.type !== 'select' || name === groupName) return false;
    const visiting = new Set();
    const complete = new Set();
    function visit(current) {
      if (current === groupName || visiting.has(current)) return false;
      if (complete.has(current) || !byName.has(current)) return true;
      visiting.add(current);
      const nested = byName.get(current).proxies;
      if (Array.isArray(nested)) {
        for (const child of nested) {
          if (!visit(child)) return false;
        }
      }
      visiting.delete(current);
      complete.add(current);
      return true;
    }
    return visit(name);
  }
  const parentNames = Array.isArray(options.followParentGroups) ? options.followParentGroups : [];
  const parents = parentNames.filter(name => typeof name === 'string' && safeParent(name));
  group.proxies = Array.from(new Set(existing.concat(nodes, parents)));
  return config;
}

function main(config) {
  return configureSelector(config, {
    groupName: 'Example Selector',
    followParentGroups: ['Parent Selector'],
    exclude: /测试|測試/i
  });
}

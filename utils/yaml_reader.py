import yaml

# Lettura della topologia
with open('topology.yaml', 'r') as f:
    topo_config = yaml.safe_load(f)
    print(topo_config['spines']) # Output: 2

# Lettura dei tenants
with open('tenants.yaml', 'r') as f:
    tenants_config = yaml.safe_load(f)
    print(tenants_config['tenants'][0]['name']) # Output: A
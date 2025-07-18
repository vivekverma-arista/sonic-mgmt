#!/usr/bin/python

from ansible.module_utils.basic import AnsibleModule
import traceback

DOCUMENTATION = '''
module: vlan_config.py
Ansible_version_added:  2.0.0.2
short_description: Gather all vlan info related to servers' interfaces
Description:
       When deploy testbed topology with server connected to TOR SONiC,
       gather server vlan interfaces info for generating SONiC minigraph file
 arguments:
    vm_topo_config: Topology file; required: True
    port_alias: Port aliases of TOR SONiC; required: True
    vlan_config:  vlan config name to use; required: False

Ansible_facts:
    'vlan_configs': all Vlans Configuration
'''

EXAMPLES = '''
    - name: find all vlan configurations for T0 topology
      vlan_config:
        vm_topo_config: "{{ vm_topo_config }}"
        port_alias: "{{ port_alias }}"
        vlan_config: "{{ vlan_config | default(None) }}"
'''


def main():
    module = AnsibleModule(
        argument_spec=dict(
            vm_topo_config=dict(required=True, type="dict"),
            port_alias=dict(required=True, type="list"),
            vlan_config=dict(required=False, type='str', default=None),
        ),
        supports_check_mode=True
    )
    m_args = module.params
    port_alias = m_args['port_alias']
    vlan_config = m_args['vlan_config']

    vlan_configs = {}
    try:
        if vlan_config is None or len(vlan_config) == 0:
            vlan_config = m_args['vm_topo_config']['DUT']['vlan_configs']['default_vlan_config']

        vlans = m_args['vm_topo_config']['DUT']['vlan_configs'][vlan_config]
        for vlan, vlan_param in vlans.items():
            vlan_configs.update({vlan: {}})
            vlan_configs[vlan]['id'] = vlan_param['id']
            vlan_configs[vlan]['tag'] = vlan_param['tag']
            if 'prefix' in vlan_param:
                vlan_configs[vlan]['prefix'] = vlan_param['prefix']
            if 'prefix_v6' in vlan_param:
                vlan_configs[vlan]['prefix_v6'] = vlan_param['prefix_v6']
            if 'secondary_subnet' in vlan_param:
                vlan_configs[vlan]['secondary_subnet'] = vlan_param['secondary_subnet']
            vlan_configs[vlan]['intfs'] = [port_alias[i]
                                           for i in vlan_param['intfs']]
            vlan_configs[vlan]['portchannels'] = vlan_param.get(
                'portchannels', [])

            if 'mac' in vlan_param:
                vlan_configs[vlan]['mac'] = vlan_param['mac']

            if 'type' in vlan_param:
                vlan_configs[vlan]['type'] = vlan_param['type']

    except Exception:
        module.fail_json(msg=traceback.format_exc())
    else:
        module.exit_json(ansible_facts={'vlan_configs': vlan_configs})


if __name__ == "__main__":
    main()

#!/usr/bin/env python3

"""Zeigt je ESXi-Host die Zuordnung VLAN -> IP-Bereich.



Ausgegeben werden:

  1. Portgruppen des Standard-vSwitch mit VLAN-ID

  2. VMkernel-Adapter mit Portgruppe und Adresse (zeigt Management-Netze)

  3. vom Host beobachtete IP-Bereiche je VLAN (Network Hints)



Die beobachteten Bereiche stammen aus mitgelesenem Verkehr auf den

physischen Netzwerkkarten. Sie sind ein Indiz, keine Vergabeliste.

Ein VLAN ohne beobachteten Bereich hatte im Beobachtungszeitraum

keinen auswertbaren Verkehr.



Voraussetzungen:

    source ~/iac_lab/secrets.env

    source ~/iac_lab/venv-wab/bin/activate



Aufruf:

    python vlan_subnets.py

"""



import os

import ssl

import sys



from pyVim.connect import SmartConnect, Disconnect

from pyVmomi import vim, vmodl





def env(*names):

    for name in names:

        value = os.environ.get(name)

        if value:

            return value

    return None





def main():

    host = env("VSPHERE_SERVER", "VMWARE_HOST", "VCENTER_HOST")

    user = env("VSPHERE_USER", "VMWARE_USER", "VCENTER_USER")

    password = env("VSPHERE_PASSWORD", "VMWARE_PASSWORD", "VCENTER_PASSWORD")

    port = int(env("VMWARE_PORT", "VCENTER_PORT") or 443)

    if not (host and user and password):

        sys.exit("Fehlende Zugangsdaten. secrets.env geladen?")



    si = SmartConnect(host=host, user=user, pwd=password, port=port,

                      sslContext=ssl._create_unverified_context())

    try:

        content = si.RetrieveContent()

        view = content.viewManager.CreateContainerView(

            content.rootFolder, [vim.HostSystem], True)

        hosts = list(view.view)

        view.Destroy()



        for esx in hosts:

            print(f"\n=== Host: {esx.name} ===")

            net = esx.config.network



            portgroups_by_vlan = {}

            print("\nPortgruppen (Standard-vSwitch):")

            for pg in net.portgroup:

                vlan = pg.spec.vlanId

                portgroups_by_vlan.setdefault(vlan, []).append(pg.spec.name)

                print(f"  VLAN {vlan:>4}  {pg.spec.name}  "

                      f"(vSwitch {pg.spec.vswitchName})")

            print("  Hinweis: VLAN 0 = ungetaggt, 4095 = Trunk")



            print("\nVMkernel-Adapter (Management-, vMotion-, Storage-Netze):")

            for vnic in net.vnic:

                ip = vnic.spec.ip

                print(f"  {vnic.device}  Portgruppe '{vnic.portgroup}'  "

                      f"{ip.ipAddress}/{ip.subnetMask}")



            print("\nBeobachtete IP-Bereiche je VLAN (Network Hints):")

            try:

                hints = esx.configManager.networkSystem.QueryNetworkHint()

            except vmodl.MethodFault as exc:

                print(f"  Abfrage fehlgeschlagen: {type(exc).__name__}: {exc.msg}")

                continue



            seen_vlans = set()

            for hint in hints:

                subnets = hint.subnet or []

                if not subnets:

                    print(f"  {hint.device}: keine beobachteten Bereiche")

                    continue

                for sub in sorted(subnets, key=lambda s: s.vlanId):

                    seen_vlans.add(sub.vlanId)

                    names = ", ".join(portgroups_by_vlan.get(sub.vlanId, ["-"]))

                    print(f"  {hint.device}  VLAN {sub.vlanId:>4}  "

                          f"{sub.ipSubnet:<20}  Portgruppe(n): {names}")



            silent = sorted(v for v in portgroups_by_vlan

                            if v not in seen_vlans and v not in (0, 4095))

            if silent:

                print("\nVLANs mit Portgruppe, aber ohne beobachteten Verkehr:")

                for vlan in silent:

                    print(f"  VLAN {vlan:>4}  "

                          f"{', '.join(portgroups_by_vlan[vlan])}")

    finally:

        Disconnect(si)





if __name__ == "__main__":

    main()

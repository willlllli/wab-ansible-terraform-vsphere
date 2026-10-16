#!/usr/bin/env python3
"""
Topologie- und Datastore-Erfassung fuer das Test-vCenter (WAB Terraform vs. Ansible).

Beantwortet drei Fragen:
  1. Welche ESXi-Hosts sieht dieses vCenter, und stehen sie in einem Cluster
     oder einzeln unter dem Datacenter?
  2. Welche Hosts haben welchen Datastore gemountet (= geteilter Speicher)?
  3. Laufen auf den Hosts fremde VMs (= potenzielle Fremdlast waehrend der Messreihen)?

Aufruf auf dem Control-Node, venv aktiviert und secrets.env geladen:
    source ~/iac_lab/venv-wab/bin/activate
    source ~/iac_lab/secrets.env
    python topologie_check.py

Gelesen werden ausschliesslich die bereits vorhandenen Ansible-Variablen
VMWARE_HOST, VMWARE_USER, VMWARE_PASSWORD, VMWARE_PORT, VMWARE_VALIDATE_CERTS.
Das Skript schreibt nichts ins vCenter, es liest nur.
"""

import os
import ssl
import sys

from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim


def verbinden():
    host = os.environ.get("VMWARE_HOST")
    user = os.environ.get("VMWARE_USER")
    pwd = os.environ.get("VMWARE_PASSWORD")
    port = int(os.environ.get("VMWARE_PORT", "443"))
    validate = os.environ.get("VMWARE_VALIDATE_CERTS", "false").lower() in ("true", "yes", "1")

    if not all([host, user, pwd]):
        sys.exit("VMWARE_HOST, VMWARE_USER und VMWARE_PASSWORD muessen gesetzt sein "
                 "(source ~/iac_lab/secrets.env).")

    if validate:
        return SmartConnect(host=host, user=user, pwd=pwd, port=port)

    # Zertifikatspruefung ist laut Setzung fuer beide Werkzeuge deaktiviert.
    try:
        return SmartConnect(host=host, user=user, pwd=pwd, port=port,
                            disableSslCertValidation=True)
    except TypeError:
        ctx = ssl._create_unverified_context()
        return SmartConnect(host=host, user=user, pwd=pwd, port=port, sslContext=ctx)


def alle_objekte(content, typ):
    view = content.viewManager.CreateContainerView(content.rootFolder, [typ], True)
    try:
        return list(view.view)
    finally:
        view.Destroy()


def gb(byte_wert):
    return byte_wert / (1024 ** 3)


def hosts_ausgeben(content):
    print("=" * 78)
    print("1. TOPOLOGIE: Datacenter, Cluster und Hosts")
    print("=" * 78)

    for dc in alle_objekte(content, vim.Datacenter):
        print("\nDatacenter: %s  (MoRef %s)" % (dc.name, dc._moId))

        for cr in dc.hostFolder.childEntity:
            if isinstance(cr, vim.ClusterComputeResource):
                art = "Cluster"
                drs = "an" if cr.configuration.drsConfig.enabled else "aus"
                zusatz = "  [DRS: %s]" % drs
            elif isinstance(cr, vim.ComputeResource):
                art = "Einzelhost (kein Cluster)"
                zusatz = ""
            else:
                continue

            print("  %s: %s%s" % (art, cr.name, zusatz))

            for host in cr.host:
                hw = host.summary.hardware
                rt = host.summary.runtime
                cpu_ghz = (hw.cpuMhz * hw.numCpuCores) / 1000.0
                vms = [vm for vm in host.vm]
                laufend = [vm for vm in vms
                           if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn]
                templates = [vm for vm in vms if vm.config and vm.config.template]

                print("    Host: %s  (MoRef %s)" % (host.name, host._moId))
                print("      Version:      ESXi %s Build %s" %
                      (host.config.product.version, host.config.product.build))
                print("      Zustand:      %s, Wartungsmodus: %s" %
                      (rt.connectionState, rt.inMaintenanceMode))
                print("      Hardware:     %d Kerne, %.1f GHz gesamt, %.0f GB RAM" %
                      (hw.numCpuCores, cpu_ghz, gb(hw.memorySize)))
                print("      VMs:          %d gesamt, davon %d eingeschaltet, %d Templates" %
                      (len(vms), len(laufend), len(templates)))
                for vm in laufend:
                    print("        laeuft: %s" % vm.name)


def datastores_ausgeben(content):
    print("\n" + "=" * 78)
    print("2. DATASTORES: Typ, Kapazitaet und mountende Hosts")
    print("=" * 78)

    for ds in alle_objekte(content, vim.Datastore):
        s = ds.summary
        im_pod = isinstance(ds.parent, vim.StoragePod)
        print("\nDatastore: %s  (MoRef %s)" % (ds.name, ds._moId))
        print("  Typ:            %s" % s.type)
        print("  Kapazitaet:     %.1f GB gesamt, %.1f GB frei" %
              (gb(s.capacity), gb(s.freeSpace)))
        print("  Erreichbar:     %s" % s.accessible)
        print("  Datastore-Cluster (SDRS): %s" %
              (("JA -> %s" % ds.parent.name) if im_pod else "nein"))

        mounts = ds.host or []
        print("  Gemountet von %d Host(s):" % len(mounts))
        for m in mounts:
            info = m.mountInfo
            print("    - %s  (zugreifbar: %s, Modus: %s)" %
                  (m.key.name, info.accessible, info.accessMode))

        vms = ds.vm or []
        fremde = [vm.name for vm in vms]
        print("  VMs auf diesem Datastore: %d%s" %
              (len(fremde), ("  -> " + ", ".join(fremde)) if fremde else ""))


def zusammenfassung(content):
    print("\n" + "=" * 78)
    print("3. KURZBEWERTUNG FUER DIE MESSREIHEN")
    print("=" * 78)

    hosts = alle_objekte(content, vim.HostSystem)
    print("\nAnzahl ESXi-Hosts in diesem vCenter: %d" % len(hosts))

    geteilt = []
    for ds in alle_objekte(content, vim.Datastore):
        if len(ds.host or []) > 1:
            geteilt.append((ds.name, [m.key.name for m in ds.host]))

    if geteilt:
        print("\nVon mehreren Hosts gemountete Datastores (potenzielle Fremdlast):")
        for name, hostliste in geteilt:
            print("  %s <- %s" % (name, ", ".join(hostliste)))
    else:
        print("\nKein Datastore ist von mehr als einem Host gemountet.")

    fremd = []
    for host in hosts:
        laufend = [vm.name for vm in host.vm
                   if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn]
        if laufend:
            fremd.append((host.name, laufend))

    if fremd:
        print("\nEingeschaltete VMs (Fremdlast pruefen):")
        for hostname, vmliste in fremd:
            print("  %s: %s" % (hostname, ", ".join(vmliste)))
    else:
        print("\nAuf keinem Host laeuft aktuell eine eingeschaltete VM.")


def main():
    si = verbinden()
    try:
        content = si.RetrieveContent()
        print("Verbunden mit: %s (%s %s, Build %s)\n" % (
            os.environ.get("VMWARE_HOST"),
            content.about.name,
            content.about.version,
            content.about.build))
        hosts_ausgeben(content)
        datastores_ausgeben(content)
        zusammenfassung(content)
    finally:
        Disconnect(si)


if __name__ == "__main__":
    main()

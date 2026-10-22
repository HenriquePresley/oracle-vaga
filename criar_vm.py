"""Cria a rede (se faltar) e tenta criar a VM grátis até ter vaga. Alterna Ampere A1 (1 OCPU/6 GB) e E2.1.Micro.
Ao conseguir, grava o IP público em ip_servidor.txt e termina."""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import oci
from oci.core import models as m

cfg = oci.config.from_file()
T = cfg["tenancy"]
net = oci.core.VirtualNetworkClient(cfg)
comp = oci.core.ComputeClient(cfg)
idc = oci.identity.IdentityClient(cfg)
CHAVE = Path(os.path.expanduser("~/.ssh/oracle_bot.pub")).read_text().strip()
SAIDA = Path(__file__).with_name("ip_servidor.txt")


def log(*a):
    print(datetime.now().strftime("%d/%m %H:%M:%S"), *a, flush=True)


def rede() -> str:
    vcns = [v for v in net.list_vcns(T, display_name="bot-vcn").data if v.lifecycle_state == "AVAILABLE"]
    if vcns:
        vcn = vcns[0]
    else:
        vcn = net.create_vcn(m.CreateVcnDetails(compartment_id=T, cidr_blocks=["10.0.0.0/16"],
                                                display_name="bot-vcn", dns_label="botvcn")).data
        vcn = oci.wait_until(net, net.get_vcn(vcn.id), "lifecycle_state", "AVAILABLE").data
        log("rede criada")
    igws = net.list_internet_gateways(T, vcn_id=vcn.id).data
    igw = igws[0] if igws else net.create_internet_gateway(m.CreateInternetGatewayDetails(
        compartment_id=T, vcn_id=vcn.id, is_enabled=True, display_name="bot-igw")).data
    igw = oci.wait_until(net, net.get_internet_gateway(igw.id), "lifecycle_state", "AVAILABLE").data
    net.update_route_table(vcn.default_route_table_id, m.UpdateRouteTableDetails(route_rules=[
        m.RouteRule(destination="0.0.0.0/0", destination_type="CIDR_BLOCK", network_entity_id=igw.id)]))
    subs = [s for s in net.list_subnets(T, vcn_id=vcn.id).data if s.lifecycle_state == "AVAILABLE"]
    sub = subs[0] if subs else net.create_subnet(m.CreateSubnetDetails(
        compartment_id=T, vcn_id=vcn.id, cidr_block="10.0.0.0/24", display_name="bot-subnet", dns_label="botsub",
        prohibit_public_ip_on_vnic=False, route_table_id=vcn.default_route_table_id,
        security_list_ids=[vcn.default_security_list_id])).data
    sub = oci.wait_until(net, net.get_subnet(sub.id), "lifecycle_state", "AVAILABLE").data
    return sub.id


def imagem(shape: str) -> str:
    return comp.list_images(T, operating_system="Canonical Ubuntu", operating_system_version="24.04", shape=shape,
                            sort_by="TIMECREATED").data[0].id


def ip_publico(inst_id: str) -> str:
    for a in comp.list_vnic_attachments(T, instance_id=inst_id).data:
        v = net.get_vnic(a.vnic_id).data
        if v.public_ip:
            return v.public_ip
    return ""


def avisar(texto: str):
    """Manda mensagem no seu privado pelo bot (só no GitHub, onde TG_TOKEN/TG_ADMIN estão nos segredos)."""
    token, admin = os.getenv("TG_TOKEN"), os.getenv("TG_ADMIN")
    if token and admin:
        import urllib.parse, urllib.request
        dados = urllib.parse.urlencode({"chat_id": admin, "text": texto}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", dados, timeout=20)


def ja_existe():
    for i in comp.list_instances(T, display_name="bot-ofertas").data:
        if i.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            return i
    return None


def main():
    ad = idc.list_availability_domains(T).data[0].name
    for _ in range(20):  # chave de API nova demora uns minutos pra valer em todos os serviços (401 de vez em quando)
        try:
            sub = rede()
            break
        except oci.exceptions.ServiceError as e:
            if e.status != 401:
                raise
            log("chave ainda ativando, tento de novo em 30 s"); time.sleep(30)
    else:
        sys.exit("a chave de API não ativou")
    falhas_401 = 0
    tipos = [("VM.Standard.A1.Flex", m.LaunchInstanceShapeConfigDetails(ocpus=1, memory_in_gbs=6)),
             ("VM.Standard.E2.1.Micro", None)]
    imgs = {s: imagem(s) for s, _ in tipos}
    tentativa = 0
    fim = time.time() + float(os.getenv("MAX_MINUTOS", "0") or 0) * 60  # 0 = sem limite (no PC)
    criou_agora = False
    while True:
        inst = ja_existe()
        if inst is None:
            if os.getenv("MAX_MINUTOS") and time.time() > fim:
                log(f"sem vaga nesta rodada ({tentativa} tentativas); a próxima rodada tenta de novo")
                return
            shape, conf = tipos[tentativa % len(tipos)]
            tentativa += 1
            try:
                inst = comp.launch_instance(m.LaunchInstanceDetails(
                    availability_domain=ad, compartment_id=T, display_name="bot-ofertas", shape=shape,
                    shape_config=conf,
                    source_details=m.InstanceSourceViaImageDetails(image_id=imgs[shape], boot_volume_size_in_gbs=50),
                    create_vnic_details=m.CreateVnicDetails(subnet_id=sub, assign_public_ip=True),
                    metadata={"ssh_authorized_keys": CHAVE})).data
                log(f"CONSEGUIU vaga em {shape}!")
                criou_agora = True
            except oci.exceptions.ServiceError as e:
                if e.status == 429:
                    log("muitas tentativas, esperando 2 min"); time.sleep(120)
                elif "capacity" in (e.message or "").lower():
                    if tentativa % 30 == 1:
                        log(f"sem vaga ainda ({shape}), tentativa {tentativa}")
                    time.sleep(30)
                elif e.status == 401 and falhas_401 < 20:
                    falhas_401 += 1
                    log("autenticação falhou (chave ativando?), tento de novo"); time.sleep(30)
                elif e.status in (401, 404) or "LimitExceeded" in (e.code or ""):
                    log("erro que não resolve tentando de novo:", e.status, e.code, e.message); sys.exit(1)
                else:
                    log("erro:", e.status, e.code, (e.message or "")[:150]); time.sleep(120)
                continue
            except Exception as e:  # rede caiu etc.
                log("falha de conexão:", e); time.sleep(60)
                continue
        inst = oci.wait_until(comp, comp.get_instance(inst.id), "lifecycle_state", "RUNNING",
                              max_wait_seconds=900).data
        ip = ip_publico(inst.id)
        SAIDA.write_text(f"{ip} {inst.shape}\n")
        log(f"PRONTA: {inst.shape} IP {ip}")
        if criou_agora:
            avisar(f"✅ Servidor da Oracle criado!\nMáquina: {inst.shape}\nIP: {ip}\n\n"
                   "Abra o Claude e diga: instala o bot no servidor.")
        return


if __name__ == "__main__":
    main()

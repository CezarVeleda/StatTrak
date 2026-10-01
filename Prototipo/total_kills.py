import argparse
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import serial

PASTA = Path(__file__).resolve().parent

PORTA = "COM3"                  # troque pela sua porta
BAUD = 9600
STEAMID = "76561198258715931"
INTERVALO_STEAM = 60            # segundos entre consultas a Steam

GSI_HOST = "127.0.0.1"
GSI_PORT = 3000

# Modos cujos kills entram em "pendentes". Ajuste depois de ver os
# valores de map.mode logados numa partida real (Premier etc. podem
# usar nomes diferentes de "competitive").
MODOS_CONTADOS = {"competitive"}

# tolerancia APOS o fim da partida (map.mode virar None) para a Steam sincronizar;
# so passado esse tempo sem sincronizar e que presumimos "modo nao contado" e zeramos.
# 10 min se mostrou curto num teste real (Steam demorou mais que isso pra sincronizar).
GAP_SINCRONIZACAO_STEAM_SEG = 30 * 60

ARQUIVO_CHAVE = PASTA / "chave.txt"
ARQUIVO_ESTADO = PASTA / "state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stattrak")


def _ler_chave():
    with open(ARQUIVO_CHAVE) as f:
        return f.read().strip()


def _url_steam(chave):
    return (
        "https://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v0002/"
        f"?appid=730&key={chave}&steamid={STEAMID}"
    )


def buscar_total_kills(chave):
    req = urllib.request.Request(_url_steam(chave))
    try:
        with urllib.request.urlopen(req, timeout=10) as resposta:
            dados = json.load(resposta)
    except Exception as erro:
        # nunca deixar a URL (contem a chave) vazar para o log
        raise RuntimeError(f"falha na consulta a Steam ({type(erro).__name__})") from None
    for stat in dados.get("playerstats", {}).get("stats", []):
        if stat["name"] == "total_kills":
            return stat["value"]
    return None


@dataclass
class Estado:
    total_steam: int = None
    pendentes: int = 0
    ultimo_kills_partida: int = 0
    ultima_kill_ts: float = None
    em_mapa: bool = None       # None = ainda nao sabemos; True = partida ativa; False = no menu
    fim_partida_ts: float = None  # quando map.mode foi visto virando None (fim de partida)


def registrar_kills_gsi(estado, kills, modo, agora, fase=None):
    """Atualiza pendentes a partir de um kill count vindo do GSI."""
    if modo not in MODOS_CONTADOS:
        return
    if fase == "warmup":
        # kill de aquecimento: a Steam nao conta isso no total_kills oficial
        return
    if kills >= estado.ultimo_kills_partida:
        delta = kills - estado.ultimo_kills_partida
    else:
        delta = kills  # kills caiu: e uma partida nova
    if delta > 0:
        estado.pendentes += delta
        estado.ultima_kill_ts = agora
    estado.ultimo_kills_partida = kills


def atualizar_fase_partida(estado, modo, agora):
    """Usa map.mode do GSI para saber se a partida esta ativa ou acabou.

    So arma o relogio de tolerancia (fim_partida_ts) quando detecta a
    transicao "tinha mapa -> nao tem mais mapa", nunca por tempo corrido,
    pra uma partida comprida (ex. com prorrogacao) nunca disparar o
    timeout no meio do jogo.
    """
    em_mapa_agora = modo is not None

    if estado.em_mapa is None:
        # primeira leitura depois de iniciar/reiniciar o script
        if not em_mapa_agora and estado.pendentes > 0:
            # ja comecamos no menu com pendentes vindos do state.json:
            # nao sabemos ha quanto tempo a partida acabou, entao damos
            # o beneficio da duvida e contamos a janela a partir de agora
            estado.fim_partida_ts = agora
    elif estado.em_mapa and not em_mapa_agora:
        estado.fim_partida_ts = agora
    elif em_mapa_agora:
        estado.fim_partida_ts = None  # partida ativa: cancela qualquer contagem

    estado.em_mapa = em_mapa_agora


def aplicar_atualizacao_steam(estado, novo_total, agora):
    """Reduz pendentes pelo quanto a Steam já confirmou."""
    if novo_total is None:
        return
    if estado.total_steam is None:
        estado.total_steam = novo_total
        return
    if novo_total > estado.total_steam:
        x = novo_total - estado.total_steam
        estado.pendentes = max(0, estado.pendentes - x)
    estado.total_steam = novo_total


def checar_timeout(estado, agora):
    """Zera pendentes se a partida acabou e a Steam nao sincronizou a tempo."""
    if (
        estado.pendentes > 0
        and estado.fim_partida_ts is not None
        and (agora - estado.fim_partida_ts) > GAP_SINCRONIZACAO_STEAM_SEG
    ):
        estado.pendentes = 0
        estado.fim_partida_ts = None
        log.warning(
            "Partida acabou e a Steam nao sincronizou em %.0f min: pendentes zerados",
            GAP_SINCRONIZACAO_STEAM_SEG / 60,
        )
        return True
    return False


def valor_display(estado):
    base = estado.total_steam if estado.total_steam is not None else 0
    return base + estado.pendentes


def processar_payload_gsi(estado, payload, agora):
    """Aplica um payload de GSI ao estado. Retorna o map.mode observado (ou None)."""
    provider = payload.get("provider") or {}
    player = payload.get("player") or {}
    mapa = payload.get("map") or {}
    modo = mapa.get("mode")
    fase_gsi = mapa.get("phase")  # ex: "warmup", "live", "intermission", "gameover"

    atualizar_fase_partida(estado, modo, agora)

    if player.get("steamid") != provider.get("steamid"):
        return modo  # espectando outro jogador

    match_stats = player.get("match_stats")
    if not match_stats:
        return modo

    kills = match_stats.get("kills")
    if kills is None:
        return modo

    registrar_kills_gsi(estado, kills, modo, agora, fase_gsi)
    return modo


def salvar_estado(estado, caminho=None):
    caminho = caminho or ARQUIVO_ESTADO
    dados = {
        "total_steam": estado.total_steam,
        "pendentes": estado.pendentes,
        "ultimo_kills_partida": estado.ultimo_kills_partida,
        "ultima_kill_ts": estado.ultima_kill_ts,
    }
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados))
    tmp.replace(caminho)


def carregar_estado(caminho=None):
    caminho = caminho or ARQUIVO_ESTADO
    estado = Estado()
    if caminho.exists():
        try:
            dados = json.loads(caminho.read_text())
            estado.total_steam = dados.get("total_steam")
            estado.pendentes = dados.get("pendentes", 0)
            estado.ultimo_kills_partida = dados.get("ultimo_kills_partida", 0)
            estado.ultima_kill_ts = dados.get("ultima_kill_ts")
        except Exception as erro:
            log.warning("Nao foi possivel carregar state.json (%s); comecando do zero", type(erro).__name__)
    return estado


class Contexto:
    """Estado compartilhado entre a thread do GSI e a thread principal (Steam)."""

    def __init__(self, estado, arduino):
        self.lock = threading.Lock()
        self.estado = estado
        self.arduino = arduino
        self.ultimo_enviado = None

    def _enviar_sem_lock(self, forcar=False):
        valor = valor_display(self.estado)
        if forcar or valor != self.ultimo_enviado:
            if self.arduino is not None:
                self.arduino.write(f"{valor}\n".encode())
            self.ultimo_enviado = valor
            log.info(
                "Display -> %d (steam=%s + pendentes=%d)",
                valor, self.estado.total_steam, self.estado.pendentes,
            )
        salvar_estado(self.estado)
        return valor

    def registrar_gsi(self, payload):
        agora = time.time()
        with self.lock:
            modo = processar_payload_gsi(self.estado, payload, agora)
            log.info("GSI recebido: map.mode=%s", modo)
            self._enviar_sem_lock()

    def registrar_steam(self, novo_total):
        agora = time.time()
        with self.lock:
            aplicar_atualizacao_steam(self.estado, novo_total, agora)
            checar_timeout(self.estado, agora)
            self._enviar_sem_lock(forcar=True)


def criar_handler(contexto):
    class GSIHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            tamanho = int(self.headers.get("Content-Length", 0))
            corpo = self.rfile.read(tamanho)
            self.send_response(200)
            self.end_headers()
            try:
                payload = json.loads(corpo)
            except json.JSONDecodeError:
                log.warning("GSI: payload invalido recebido")
                return
            contexto.registrar_gsi(payload)

        def log_message(self, format, *args):
            pass  # silencia o log padrao do http.server

    return GSIHandler


def iniciar_servidor_gsi(contexto):
    servidor = ThreadingHTTPServer((GSI_HOST, GSI_PORT), criar_handler(contexto))
    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    log.info("Servidor GSI escutando em http://%s:%d", GSI_HOST, GSI_PORT)
    return servidor


def abrir_arduino():
    try:
        arduino = serial.Serial(PORTA, BAUD, timeout=1)
        time.sleep(2)  # o Uno reinicia ao abrir a porta
        log.info("Arduino conectado em %s", PORTA)
        return arduino
    except Exception as erro:
        log.warning("Nao foi possivel abrir %s (%s); rodando sem Arduino", PORTA, type(erro).__name__)
        return None


def loop_steam(contexto, chave):
    while True:
        try:
            total = buscar_total_kills(chave)
            contexto.registrar_steam(total)
        except Exception as erro:
            log.warning("Falha ao consultar a Steam: %s", erro)
        time.sleep(INTERVALO_STEAM)


def payload_gsi_teste(kills, modo="competitive"):
    return {
        "provider": {"steamid": STEAMID},
        "map": {"mode": modo},
        "player": {
            "steamid": STEAMID,
            "match_stats": {"kills": kills},
        },
    }


def simular(contexto):
    sequencia = [1, 2, 3, 1]  # o ultimo "1" simula uma partida nova (kills caiu)
    print("--- Simulacao GSI (sem abrir o jogo) ---")
    for kills in sequencia:
        dados = json.dumps(payload_gsi_teste(kills)).encode()
        req = urllib.request.Request(
            f"http://{GSI_HOST}:{GSI_PORT}/",
            data=dados,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).read()
        print(
            f"kills GSI={kills} -> pendentes={contexto.estado.pendentes} "
            f"-> valor enviado ao Uno={valor_display(contexto.estado)}"
        )
        time.sleep(0.2)
    print("--- Fim da simulacao ---")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--simular", action="store_true",
        help="Envia POSTs de GSI falsos ao proprio servidor para testar a logica de pendentes, sem abrir o jogo nem precisar do Arduino",
    )
    args = parser.parse_args()

    estado = carregar_estado()
    arduino = None if args.simular else abrir_arduino()
    contexto = Contexto(estado, arduino)
    servidor = iniciar_servidor_gsi(contexto)

    if args.simular:
        simular(contexto)
        servidor.shutdown()
        return

    chave = _ler_chave()
    print("Rodando. Ctrl+C para sair.")
    try:
        loop_steam(contexto, chave)
    except KeyboardInterrupt:
        print("Encerrado.")
    finally:
        servidor.shutdown()
        if arduino is not None:
            arduino.close()


if __name__ == "__main__":
    main()

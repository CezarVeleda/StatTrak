import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import total_kills as tk


class TestPendentes(unittest.TestCase):
    def setUp(self):
        self.estado = tk.Estado(total_steam=100)

    def test_soma_durante_a_partida(self):
        tk.registrar_kills_gsi(self.estado, 1, "competitive", agora=1001.0)
        tk.registrar_kills_gsi(self.estado, 2, "competitive", agora=1002.0)
        tk.registrar_kills_gsi(self.estado, 3, "competitive", agora=1003.0)
        self.assertEqual(self.estado.pendentes, 3)
        self.assertEqual(self.estado.ultimo_kills_partida, 3)

    def test_partida_nova_kills_cai(self):
        tk.registrar_kills_gsi(self.estado, 3, "competitive", agora=1001.0)
        tk.registrar_kills_gsi(self.estado, 1, "competitive", agora=1002.0)  # nova partida
        self.assertEqual(self.estado.pendentes, 4)  # 3 + 1
        self.assertEqual(self.estado.ultimo_kills_partida, 1)

    def test_modo_nao_contado_e_ignorado(self):
        tk.registrar_kills_gsi(self.estado, 5, "deathmatch", agora=1001.0)
        self.assertEqual(self.estado.pendentes, 0)
        self.assertEqual(self.estado.ultimo_kills_partida, 0)

    def test_reducao_quando_steam_atualiza(self):
        self.estado.pendentes = 4
        tk.aplicar_atualizacao_steam(self.estado, novo_total=102, agora=2000.0)
        self.assertEqual(self.estado.pendentes, 2)  # 4 - (102-100)
        self.assertEqual(self.estado.total_steam, 102)

    def test_reducao_nao_fica_negativa(self):
        self.estado.pendentes = 1
        tk.aplicar_atualizacao_steam(self.estado, novo_total=110, agora=2000.0)
        self.assertEqual(self.estado.pendentes, 0)

    def test_valor_display(self):
        self.estado.pendentes = 3
        self.assertEqual(tk.valor_display(self.estado), 103)

    def test_valor_display_sem_total_steam_ainda(self):
        estado = tk.Estado(pendentes=2)
        self.assertEqual(tk.valor_display(estado), 2)


class TestFaseDaPartidaETimeout(unittest.TestCase):
    def test_partida_longa_nao_dispara_timeout(self):
        # regressao do bug real: 50 min de partida ativa (map.mode sempre
        # preenchido) nao pode zerar pendentes so por passar do tempo.
        estado = tk.Estado(total_steam=100, pendentes=21)
        agora = 0.0
        for _ in range(50):
            tk.atualizar_fase_partida(estado, "competitive", agora)
            tk.checar_timeout(estado, agora)
            agora += 60.0  # 50 ciclos de 1 min = 50 min "em partida"
        self.assertEqual(estado.pendentes, 21)
        self.assertIsNone(estado.fim_partida_ts)

    def test_fim_de_partida_arma_relogio_e_timeout_dispara_depois(self):
        estado = tk.Estado(total_steam=100, pendentes=21)
        tk.atualizar_fase_partida(estado, "competitive", agora=0.0)
        tk.atualizar_fase_partida(estado, None, agora=10.0)  # partida acabou
        self.assertEqual(estado.fim_partida_ts, 10.0)

        # dentro da janela: nao dispara
        tk.checar_timeout(estado, agora=10.0 + tk.GAP_SINCRONIZACAO_STEAM_SEG - 1)
        self.assertEqual(estado.pendentes, 21)

        # passou da janela sem a steam sincronizar: dispara
        disparou = tk.checar_timeout(estado, agora=10.0 + tk.GAP_SINCRONIZACAO_STEAM_SEG + 1)
        self.assertTrue(disparou)
        self.assertEqual(estado.pendentes, 0)

    def test_nova_partida_cancela_o_relogio(self):
        estado = tk.Estado(total_steam=100, pendentes=5)
        tk.atualizar_fase_partida(estado, "competitive", agora=0.0)
        tk.atualizar_fase_partida(estado, None, agora=10.0)  # fim de partida
        self.assertIsNotNone(estado.fim_partida_ts)
        tk.atualizar_fase_partida(estado, "competitive", agora=20.0)  # partida nova comecou
        self.assertIsNone(estado.fim_partida_ts)

        # mesmo bem depois, sem fim_partida_ts nao dispara
        disparou = tk.checar_timeout(estado, agora=20.0 + tk.GAP_SINCRONIZACAO_STEAM_SEG + 100)
        self.assertFalse(disparou)
        self.assertEqual(estado.pendentes, 5)

    def test_restart_no_menu_com_pendentes_arma_relogio_na_primeira_leitura(self):
        estado = tk.Estado(total_steam=100, pendentes=3)  # veio do state.json
        tk.atualizar_fase_partida(estado, None, agora=500.0)
        self.assertEqual(estado.fim_partida_ts, 500.0)

    def test_restart_sem_pendentes_nao_arma_relogio(self):
        estado = tk.Estado(total_steam=100, pendentes=0)
        tk.atualizar_fase_partida(estado, None, agora=500.0)
        self.assertIsNone(estado.fim_partida_ts)


class TestProcessarPayloadGsi(unittest.TestCase):
    def setUp(self):
        self.estado = tk.Estado(total_steam=0)

    def test_ignora_espectador(self):
        payload = {
            "provider": {"steamid": "111"},
            "map": {"mode": "competitive"},
            "player": {"steamid": "999", "match_stats": {"kills": 5}},
        }
        modo = tk.processar_payload_gsi(self.estado, payload, agora=1.0)
        self.assertEqual(modo, "competitive")
        self.assertEqual(self.estado.pendentes, 0)

    def test_ignora_sem_match_stats(self):
        payload = {
            "provider": {"steamid": "111"},
            "map": {"mode": "competitive"},
            "player": {"steamid": "111"},
        }
        tk.processar_payload_gsi(self.estado, payload, agora=1.0)
        self.assertEqual(self.estado.pendentes, 0)

    def test_conta_quando_steamid_bate(self):
        payload = {
            "provider": {"steamid": "111"},
            "map": {"mode": "competitive"},
            "player": {"steamid": "111", "match_stats": {"kills": 2}},
        }
        modo = tk.processar_payload_gsi(self.estado, payload, agora=1.0)
        self.assertEqual(modo, "competitive")
        self.assertEqual(self.estado.pendentes, 2)


class TestPersistencia(unittest.TestCase):
    def test_salvar_e_carregar(self):
        with TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "state.json"
            estado = tk.Estado(
                total_steam=250, pendentes=7, ultimo_kills_partida=4, ultima_kill_ts=123.0,
            )
            tk.salvar_estado(estado, caminho)
            recarregado = tk.carregar_estado(caminho)
            self.assertEqual(recarregado.total_steam, 250)
            self.assertEqual(recarregado.pendentes, 7)
            self.assertEqual(recarregado.ultimo_kills_partida, 4)
            self.assertEqual(recarregado.ultima_kill_ts, 123.0)

    def test_carregar_sem_arquivo_retorna_estado_zerado(self):
        with TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "nao_existe.json"
            estado = tk.carregar_estado(caminho)
            self.assertIsNone(estado.total_steam)
            self.assertEqual(estado.pendentes, 0)


if __name__ == "__main__":
    unittest.main()

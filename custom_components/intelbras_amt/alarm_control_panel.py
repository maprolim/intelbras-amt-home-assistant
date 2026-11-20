"""Entidade Alarm Control Panel para Intelbras AMT 2018 / 4010."""

import logging
import time
from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

# Importa da biblioteca local
from .lib.server import AMTServer
from .lib.protocol.commands import ActivationCommand, DeactivationCommand
from .lib.const import PartitionCode, ResponseCode

from .const import DOMAIN, CONF_PASSWORD, ATTR_CONNECTED, ATTR_LAST_HEARTBEAT
from .coordinator import AMTCoordinator

_LOGGER = logging.getLogger(__name__)

_SIREN_TRIGGER_FALLBACK_SECONDS = 4.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configura a entidade alarm_control_panel."""
    coordinator: AMTCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([IntelbrasAMTAlarm(hass, entry, coordinator)])


class IntelbrasAMTAlarm(CoordinatorEntity[AMTCoordinator], AlarmControlPanelEntity):
    """Representa o painel de alarme Intelbras AMT 2018 / 4010."""

    _attr_has_entity_name = True
    _attr_name = "Alarme"
    _attr_code_format = CodeFormat.NUMBER
    # O teclado aparece ao armar e ao desarmar. A senha digitada é conferida
    # pela central: para desarmar é sempre obrigatória; para armar, se vier
    # vazia (ex.: automações), usamos a senha salva na configuração.
    _attr_code_arm_required = True
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_HOME |
        AlarmControlPanelEntityFeature.ARM_AWAY
        # AlarmControlPanelEntityFeature.TRIGGER  # Quando implementarmos
    )

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: AMTCoordinator,
    ) -> None:
        """Inicializa a entidade."""
        super().__init__(coordinator)
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_alarm"
        self._siren_on_since: float | None = None
        self._siren_trigger_confirmed = False
        self._last_trigger_reason: str | None = None
        self._last_ignored_triggered_decision: tuple | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Atualiza o fallback por sirene apenas com status novo da central."""
        self._update_siren_trigger_fallback()
        super()._handle_coordinator_update()

    def _update_siren_trigger_fallback(self) -> None:
        """Confirma sirene como disparo só após status sustentado."""
        status = self.coordinator.data
        if not status or not status.siren_on:
            self._siren_on_since = None
            self._siren_trigger_confirmed = False
            return

        now = time.monotonic()
        if self._siren_on_since is None:
            self._siren_on_since = now
            self._siren_trigger_confirmed = False
            return

        self._siren_trigger_confirmed = (
            now - self._siren_on_since >= _SIREN_TRIGGER_FALLBACK_SECONDS
        )

    def _trigger_reason(self) -> str | None:
        """Retorna o motivo para expor TRIGGERED, se houver."""
        status = self.coordinator.data
        if not status:
            return None

        if status.triggered and status.armed:
            # O arme global não confirma abertura em uma partição desarmada.
            # ponytail: memória antiga ainda passa; distinguir ocorrências exige
            # dados de eventos e associação zona/partição.
            if (
                not status.partitions.partitions_enabled
                or status.zones.violated_zones
            ):
                return "triggered_armed"
        if status.triggered and status.siren_on:
            return "triggered_with_siren"
        if self._siren_trigger_confirmed:
            return "siren_sustained"
        return None

    def _function_byte(self) -> int | None:
        """Retorna o byte de funcionamento do status bruto, se disponível."""
        status = self.coordinator.data
        if not status:
            return None

        raw_data = status.raw_data
        if len(raw_data) == 43:
            return raw_data[22]
        if len(raw_data) == 54:
            return raw_data[29]
        return None

    def _log_trigger_decision(self, reason: str) -> None:
        """Loga por que o painel foi exposto como TRIGGERED."""
        if reason == self._last_trigger_reason:
            return

        self._last_trigger_reason = reason
        self._last_ignored_triggered_decision = None
        status = self.coordinator.data
        if not status:
            return

        _LOGGER.warning(
            "Alarme exposto como TRIGGERED: reason=%s armed=%s triggered=%s "
            "siren_on=%s func_byte=%s open_zones=%s violated_zones=%s raw=%s",
            reason,
            status.armed,
            status.triggered,
            status.siren_on,
            self._format_byte(self._function_byte()),
            sorted(status.zones.open_zones),
            sorted(status.zones.violated_zones),
            status.raw_data.hex(" "),
        )

    def _log_ignored_triggered(self) -> None:
        """Loga quando o bit triggered é ignorado por não parecer disparo real."""
        status = self.coordinator.data
        if not status or not status.triggered:
            self._last_ignored_triggered_decision = None
            return

        # Relógio e aberturas não mudam a decisão sobre memória de disparo.
        decision = (status.armed, status.siren_on, status.partitions.partitions_enabled,
                    tuple(sorted(status.zones.violated_zones)))
        if decision == self._last_ignored_triggered_decision:
            return

        self._last_ignored_triggered_decision = decision
        reason = (
            "particionamento ativo sem zona violada"
            if status.armed
            else "central desarmada e sirene desligada"
        )
        _LOGGER.warning(
            "Ignorando bit triggered: %s; "
            "armed=%s triggered=%s siren_on=%s func_byte=%s open_zones=%s "
            "violated_zones=%s partitions=%s raw=%s",
            reason,
            status.armed,
            status.triggered,
            status.siren_on,
            self._format_byte(self._function_byte()),
            sorted(status.zones.open_zones),
            sorted(status.zones.violated_zones),
            status.partitions,
            status.raw_data.hex(" "),
        )

    @staticmethod
    def _format_byte(value: int | None) -> str | None:
        """Formata byte para log."""
        if value is None:
            return None
        return f"0x{value:02X}"

    @property
    def device_info(self):
        """Informações do dispositivo."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Intelbras AMT 2018 / 4010",
            "manufacturer": "Intelbras",
            "model": "AMT 2018 / 4010",
        }

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Retorna o estado atual do alarme usando a nova API."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        if not entry_data.get("connected", False):
            return None
        
        if not self.coordinator.data:
            return None
        
        status = self.coordinator.data
        
        trigger_reason = self._trigger_reason()
        if trigger_reason:
            self._log_trigger_decision(trigger_reason)
            return AlarmControlPanelState.TRIGGERED

        self._last_trigger_reason = None
        if status.triggered:
            self._log_ignored_triggered()
        else:
            self._last_ignored_triggered_decision = None
        
        # Verifica se está armada
        if status.armed:
            # Verifica se é stay mode (home) ou away
            if status.partitions.partitions_enabled:
                # Se partições estão habilitadas, verifica se alguma está em stay mode
                # Por enquanto, assumimos que se todas estão armadas, é away
                return AlarmControlPanelState.ARMED_AWAY
            else:
                # Sem partições, verifica se é stay mode
                # Por enquanto, assumimos away se armada
                return AlarmControlPanelState.ARMED_AWAY
        
        return AlarmControlPanelState.DISARMED

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Atributos extras."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        attrs = {
            ATTR_CONNECTED: entry_data.get("connected", False),
            "connection_id": entry_data.get("connection_id"),
        }
        
        if self.coordinator.data:
            status = self.coordinator.data
            attrs.update({
                "sirene": "Ligada" if status.siren_on else "Desligada",
                "particoes_habilitadas": status.partitions.partitions_enabled,
                "particao_a": "Armada" if status.partitions.partition_a_armed else "Desarmada",
                "particao_b": "Armada" if status.partitions.partition_b_armed else "Desarmada",
            })
        
        return attrs

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Arma o alarme no modo away (todas as zonas)."""
        await self._arm_alarm(code, partition=None)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Arma o alarme no modo home (stay)."""
        await self._arm_alarm(code, partition=PartitionCode.STAY_MODE)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Desarma o alarme (a senha digitada é sempre obrigatória)."""
        password = self._resolve_password(code, require_code=True)
        await self._send_and_check(
            DeactivationCommand.disarm_all(password),
            action="desarmar",
        )

    async def _arm_alarm(
        self,
        code: str | None = None,
        partition: PartitionCode | None = None,
    ) -> None:
        """Envia comando de ativação para a central.

        Ao armar, a senha digitada é usada quando presente; se vier vazia
        (ex.: automações), usamos a senha salva na configuração.
        """
        password = self._resolve_password(code, require_code=False)

        if partition:
            cmd = ActivationCommand(password=password, partition=partition)
        else:
            cmd = ActivationCommand.arm_all(password=password)

        await self._send_and_check(cmd, action="armar")

    def _resolve_password(self, code: str | None, *, require_code: bool) -> str:
        """Define a senha a enviar à central, sem registrá-la em log.

        - require_code=True (desarme): a senha digitada é obrigatória.
        - require_code=False (arme): usa a senha digitada; se vier vazia,
          cai para a senha salva na configuração.

        A senha é sempre tratada como texto de dígitos (nunca convertida para
        número), preservando zeros à esquerda.
        """
        typed = (code or "").strip()
        if typed:
            password = typed
        elif require_code:
            raise HomeAssistantError("É necessário digitar a senha para desarmar.")
        else:
            entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
            password = entry_data.get("password", "")

        if not (password.isascii() and password.isdigit() and 4 <= len(password) <= 6):
            raise HomeAssistantError("Senha inválida: use de 4 a 6 dígitos.")
        return password

    async def _send_and_check(self, command, *, action: str) -> None:
        """Envia um comando à central, confere a resposta e sinaliza erros.

        Levanta HomeAssistantError (exibido na interface do HA) quando a central
        recusa o comando, não responde ou está offline. Nunca registra a senha.
        """
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        server: AMTServer = entry_data.get("server")
        connection_id = entry_data.get("connection_id")

        if not server or not connection_id:
            raise HomeAssistantError(
                f"Central não conectada; não foi possível {action}."
            )

        try:
            response = await server.send_command(
                connection_id,
                command.build_net_frame(),
                wait_response=True,
            )
        except TimeoutError as err:
            _LOGGER.error("Timeout ao %s: sem resposta da central", action)
            raise HomeAssistantError(
                f"Sem resposta da central ao {action} (timeout)."
            ) from err
        except HomeAssistantError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Erro ao %s: %s", action, err)
            raise HomeAssistantError(
                f"Falha de comunicação com a central ao {action}."
            ) from err

        if response is None or not response.is_success:
            if response is not None and response.error_code == ResponseCode.NACK_WRONG_PASSWORD:
                _LOGGER.warning("Central recusou %s: senha incorreta", action)
                raise HomeAssistantError("Senha recusada pela central.")
            message = response.message if response is not None else "sem resposta"
            _LOGGER.warning("Central recusou %s: %s", action, message)
            raise HomeAssistantError(
                f"Central recusou o comando ao {action} ({message})."
            )

        _LOGGER.info("Comando de %s confirmado pela central", action)

"""Sensor platform for frigate."""

from __future__ import annotations

from collections.abc import Callable
import datetime
import json
import logging
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_URL,
    PERCENTAGE,
    UnitOfSoundPressure,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import (
    FrigateDataUpdateCoordinator,
    FrigateEntity,
    FrigateMQTTEntity,
    ReceiveMessage,
    get_attribute_classification_models_and_base_objects,
    get_cameras,
    get_cameras_zones_and_objects,
    get_classification_models_and_cameras,
    get_friendly_name,
    get_frigate_device_identifier,
    get_frigate_entity_unique_id,
    get_known_plates,
    get_object_classification_models_and_cameras,
    get_object_classification_models_cameras_and_zones,
    get_sublabel_classification_models_and_base_objects,
    get_zones,
    verify_frigate_version,
)
from .const import (
    ATTR_CLIENT,
    ATTR_CONFIG,
    ATTR_COORDINATOR,
    CONF_ENABLE_ATTRIBUTE_SENSORS,
    CONF_ENABLE_ATTRIBUTE_TRACKING,
    CONF_ENABLE_SUBLABEL_SENSORS,
    DOMAIN,
    FPS,
    MS,
    NAME,
)
from .icons import (
    ICON_CORAL,
    ICON_FACE,
    ICON_LICENSE_PLATE,
    ICON_SERVER,
    ICON_SPEEDOMETER,
    ICON_UPTIME,
    ICON_WAVEFORM,
    get_icon_from_type,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def _create_global_face_sensors(
    entry: ConfigEntry,
    frigate_config: dict[str, Any],
    client: Any,
    entities: list[FrigateEntity],
) -> None:
    """Create global face sensors."""
    if not frigate_config.get("face_recognition", {}).get("enabled"):
        return
    try:
        known_faces = await client.async_get_faces()
        entities.extend(
            [
                FrigateGlobalFaceSensor(entry, frigate_config, face_name)
                for face_name in known_faces
            ]
        )
    except Exception:
        _LOGGER.warning(
            "Failed to fetch known faces from API. Global face sensors will not be created."
        )


async def _create_global_plate_sensors(
    entry: ConfigEntry,
    frigate_config: dict[str, Any],
    entities: list[FrigateEntity],
) -> None:
    """Create global plate sensors."""
    if not frigate_config.get("lpr", {}).get("enabled"):
        return
    known_plates = get_known_plates(frigate_config)
    entities.extend(
        [
            FrigateGlobalPlateSensor(entry, frigate_config, plate_name)
            for plate_name in known_plates
        ]
    )


async def _create_global_object_classification_sensors(
    entry: ConfigEntry,
    frigate_config: dict[str, Any],
    client: Any,
    entities: list[FrigateEntity],
) -> None:
    """Create global object classification sensors."""
    classification_config = frigate_config.get("classification", {}).get("custom", {})
    for model_key, model_config in classification_config.items():
        object_config = model_config.get("object_config")
        if object_config:
            try:
                classes = await client.async_get_classification_model_classes(model_key)
                entities.extend(
                    [
                        FrigateGlobalObjectClassificationSensor(
                            entry, frigate_config, model_key, class_name
                        )
                        for class_name in classes
                    ]
                )
            except Exception:
                _LOGGER.warning(
                    "Failed to fetch classification classes for model %s. "
                    "Global object classification sensors will not be created for this model.",
                    model_key,
                )


def _build_zone_to_camera_map(frigate_config: dict[str, Any]) -> dict[str, str]:
    """Build a mapping of zone names to their parent camera name."""
    zone_to_camera: dict[str, str] = {}
    for cam_name, cam_config in frigate_config.get("cameras", {}).items():
        for zone_name in cam_config.get("zones", {}):
            zone_to_camera[zone_name] = cam_name
    return zone_to_camera


async def _create_sublabel_sensors(
    entry: ConfigEntry,
    frigate_config: dict[str, Any],
    client: Any,
    entities: list[FrigateEntity],
) -> None:
    """Create count sensors for sublabel classifications."""
    sublabel_models = get_sublabel_classification_models_and_base_objects(frigate_config)
    zone_to_camera = _build_zone_to_camera_map(frigate_config)

    for model_key, base_objects in sublabel_models.items():
        try:
            classes = await client.async_get_classification_model_classes(model_key)

            for sublabel_class in classes:
                for cam_name, obj_name in get_cameras_zones_and_objects(frigate_config):
                    if obj_name in base_objects:
                        # Resolve the actual camera name for zone sensors
                        actual_cam = zone_to_camera.get(cam_name, cam_name)
                        entities.append(
                            FrigateSublabelCountSensor(
                                entry,
                                frigate_config,
                                cam_name,
                                obj_name,
                                model_key,
                                sublabel_class,
                                actual_cam_name=actual_cam,
                            )
                        )
        except Exception:
            _LOGGER.warning(
                "Failed to fetch sublabel classes for model %s. "
                "Sublabel count sensors will not be created for this model.",
                model_key,
            )


async def _create_attribute_sensors(
    entry: ConfigEntry,
    frigate_config: dict[str, Any],
    client: Any,
    entities: list[FrigateEntity],
) -> None:
    """Create count sensors for attribute classifications."""
    attribute_models = get_attribute_classification_models_and_base_objects(frigate_config)
    zone_to_camera = _build_zone_to_camera_map(frigate_config)

    for model_key, base_objects in attribute_models.items():
        try:
            classes = await client.async_get_classification_model_classes(model_key)

            for attribute_class in classes:
                for cam_name, obj_name in get_cameras_zones_and_objects(frigate_config):
                    if obj_name in base_objects:
                        actual_cam = zone_to_camera.get(cam_name, cam_name)
                        entities.append(
                            FrigateAttributeCountSensor(
                                entry,
                                frigate_config,
                                cam_name,
                                obj_name,
                                model_key,
                                attribute_class,
                                actual_cam_name=actual_cam,
                            )
                        )
        except Exception:
            _LOGGER.warning(
                "Failed to fetch attribute classes for model %s. "
                "Attribute count sensors will not be created for this model.",
                model_key,
            )



async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Sensor entry setup."""
    frigate_config: dict[str, Any] = hass.data[DOMAIN][entry.entry_id][ATTR_CONFIG]
    coordinator = hass.data[DOMAIN][entry.entry_id][ATTR_COORDINATOR]
    client = hass.data[DOMAIN][entry.entry_id][ATTR_CLIENT]

    entities: list[FrigateEntity] = []

    # Create main Frigate device entities first, before any camera entities
    # that reference it via via_device to avoid device registry warnings
    entities.append(FrigateStatusSensor(coordinator, entry))
    entities.append(FrigateUptimeSensor(coordinator, entry))

    for key, value in coordinator.data.items():

        if key.endswith("_fps"):
            entities.append(
                FrigateFpsSensor(coordinator, entry, fps_type=key.removesuffix("_fps"))
            )
        elif key == "detectors":
            for name in value.keys():
                entities.append(DetectorSpeedSensor(coordinator, entry, name))
        elif key == "gpu_usages":
            for name in value.keys():
                entities.append(GpuLoadSensor(coordinator, entry, name))
        elif key == "processes":
            # don't create sensor for other processes
            continue
        elif key == "service":
            # Temperature is only supported on PCIe Coral.
            for name in value.get("temperatures", {}):
                entities.append(DeviceTempSensor(coordinator, entry, name))
        elif key == "cpu_usages":
            for camera in get_cameras(frigate_config):
                entities.append(
                    CameraProcessCpuSensor(coordinator, entry, camera, "capture")
                )
                entities.append(
                    CameraProcessCpuSensor(coordinator, entry, camera, "detect")
                )
                entities.append(
                    CameraProcessCpuSensor(coordinator, entry, camera, "ffmpeg")
                )
        elif key == "cameras":
            for name, cam in value.items():
                entities.extend(
                    CameraFpsSensor(coordinator, entry, name, k.removesuffix("_fps"))
                    for k in cam
                    if k.endswith("_fps")
                )

                if frigate_config["cameras"][name]["audio"]["enabled_in_config"]:
                    entities.append(CameraSoundSensor(coordinator, entry, name))

    frigate_config = hass.data[DOMAIN][entry.entry_id][ATTR_CONFIG]
    enable_attribute_tracking = entry.options.get(CONF_ENABLE_ATTRIBUTE_TRACKING, True)
    
    entities.extend(
        [
            FrigateReviewStatusSensor(entry, frigate_config, cam_name)
            for cam_name in get_cameras(frigate_config)
        ]
    )
    entities.extend(
        [
            FrigateObjectCountSensor(entry, frigate_config, cam_name, obj, enable_attribute_tracking)
            for cam_name, obj in get_cameras_zones_and_objects(frigate_config)
        ]
    )
    entities.extend(
        [
            FrigateActiveObjectCountSensor(entry, frigate_config, cam_name, obj)
            for cam_name, obj in get_cameras_zones_and_objects(frigate_config)
        ]
    )

    if verify_frigate_version(frigate_config, "0.16"):
        if frigate_config.get("face_recognition", {}).get("enabled"):
            entities.extend(
                [
                    FrigateRecognizedFaceSensor(entry, frigate_config, cam_name)
                    for cam_name, cam_config in frigate_config["cameras"].items()
                    if cam_config.get("face_recognition", {}).get("enabled")
                ]
            )

        if frigate_config.get("lpr", {}).get("enabled"):
            entities.extend(
                [
                    FrigateRecognizedPlateSensor(entry, frigate_config, cam_name)
                    for cam_name, cam_config in frigate_config["cameras"].items()
                    if cam_config.get("lpr", {}).get("enabled")
                ]
            )

    if verify_frigate_version(frigate_config, "0.17"):
        entities.extend(
            [
                FrigateClassificationSensor(entry, frigate_config, cam_name, model_key)
                for cam_name, model_key in get_classification_models_and_cameras(
                    frigate_config
                )
            ]
        )
        entities.extend(
            [
                FrigateObjectClassificationSensor(
                    entry, frigate_config, cam_or_zone_name, model_key, actual_cam_name
                )
                for cam_or_zone_name, model_key, actual_cam_name in get_object_classification_models_cameras_and_zones(
                    frigate_config
                )
            ]
        )

        # Global sensors
        await _create_global_face_sensors(entry, frigate_config, client, entities)
        await _create_global_plate_sensors(entry, frigate_config, entities)
        await _create_global_object_classification_sensors(
            entry, frigate_config, client, entities
        )
        
        # Sublabel sensors (create count sensors for each sublabel class)
        # Only create if the option is enabled (defaults to True)
        if entry.options.get(CONF_ENABLE_SUBLABEL_SENSORS, True):
            await _create_sublabel_sensors(entry, frigate_config, client, entities)
        
        # Attribute sensors (create count sensors for each attribute class)
        # Only create if the option is enabled (defaults to True)
        if entry.options.get(CONF_ENABLE_ATTRIBUTE_SENSORS, True):
            await _create_attribute_sensors(entry, frigate_config, client, entities)

    async_add_entities(entities)


class FrigateFpsSensor(
    FrigateEntity, CoordinatorEntity[FrigateDataUpdateCoordinator], SensorEntity
):
    """Frigate Sensor class."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: FrigateDataUpdateCoordinator,
        config_entry: ConfigEntry,
        fps_type: str = "detection",
    ) -> None:
        """Construct a FrigateFpsSensor."""
        FrigateEntity.__init__(self, config_entry)
        CoordinatorEntity.__init__(self, coordinator)
        self._fps_type = fps_type
        self._attr_entity_registry_enabled_default = False

    @property
    def name(self) -> str:
        return f"{self._fps_type} fps"

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id, "sensor_fps", self._fps_type
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {get_frigate_device_identifier(self._config_entry)},
            "name": NAME,
            "model": self._get_model(),
            "configuration_url": self._config_entry.data.get(CONF_URL),
            "manufacturer": NAME,
        }

    @property
    def native_value(self) -> int | None:
        """Return the value of the sensor."""
        if self.coordinator.data:
            data = self.coordinator.data.get(f"{self._fps_type}_fps")
            if data is not None:
                try:
                    return round(float(data))
                except ValueError:
                    pass
        return None

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the native unit of measurement of the sensor."""
        return FPS

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return ICON_SPEEDOMETER


class FrigateStatusSensor(
    FrigateEntity, CoordinatorEntity[FrigateDataUpdateCoordinator], SensorEntity
):
    """Frigate Status Sensor class."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Status"

    def __init__(
        self, coordinator: FrigateDataUpdateCoordinator, config_entry: ConfigEntry
    ) -> None:
        """Construct a FrigateStatusSensor."""
        FrigateEntity.__init__(self, config_entry)
        CoordinatorEntity.__init__(self, coordinator)
        self._attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id, "sensor_status", "frigate"
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {get_frigate_device_identifier(self._config_entry)},
            "name": NAME,
            "model": self._get_model(),
            "configuration_url": self._config_entry.data.get(CONF_URL),
            "manufacturer": NAME,
        }

    @property
    def native_value(self) -> str:
        """Return the value of the sensor."""
        return str(self.coordinator.server_status)

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return ICON_SERVER


class FrigateUptimeSensor(
    FrigateEntity, CoordinatorEntity[FrigateDataUpdateCoordinator], SensorEntity
):
    """Frigate Uptime Sensor class."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_name = "Uptime"

    def __init__(
        self, coordinator: FrigateDataUpdateCoordinator, config_entry: ConfigEntry
    ) -> None:
        """Construct a FrigateUptimeSensor."""
        FrigateEntity.__init__(self, config_entry)
        CoordinatorEntity.__init__(self, coordinator)
        self._attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id, "uptime", "frigate"
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {get_frigate_device_identifier(self._config_entry)},
            "name": NAME,
            "model": self._get_model(),
            "configuration_url": self._config_entry.data.get(CONF_URL),
            "manufacturer": NAME,
        }

    @property
    def native_value(self) -> int | None:
        """Return the value of the sensor."""
        if self.coordinator.data:
            data = self.coordinator.data.get("service", {}).get("uptime", 0)
            try:
                return int(data)
            except (TypeError, ValueError):
                pass
        return None

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the native unit of measurement of the sensor."""
        return UnitOfTime.SECONDS

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return ICON_UPTIME


class DetectorSpeedSensor(
    FrigateEntity, CoordinatorEntity[FrigateDataUpdateCoordinator], SensorEntity
):
    """Frigate Detector Speed class."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.DURATION

    def __init__(
        self,
        coordinator: FrigateDataUpdateCoordinator,
        config_entry: ConfigEntry,
        detector_name: str,
    ) -> None:
        """Construct a DetectorSpeedSensor."""
        FrigateEntity.__init__(self, config_entry)
        CoordinatorEntity.__init__(self, coordinator)
        self._detector_name = detector_name
        self._attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id, "sensor_detector_speed", self._detector_name
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {get_frigate_device_identifier(self._config_entry)},
            "name": NAME,
            "model": self._get_model(),
            "configuration_url": self._config_entry.data.get(CONF_URL),
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"{get_friendly_name(self._detector_name)} inference speed"

    @property
    def native_value(self) -> int | None:
        """Return the value of the sensor."""
        if self.coordinator.data:
            data = (
                self.coordinator.data.get("detectors", {})
                .get(self._detector_name, {})
                .get("inference_speed")
            )
            if data is not None:
                try:
                    return round(float(data))
                except ValueError:
                    pass
        return None

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the native unit of measurement of the sensor."""
        return MS

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return ICON_SPEEDOMETER


class GpuLoadSensor(
    FrigateEntity, CoordinatorEntity[FrigateDataUpdateCoordinator], SensorEntity
):
    """Frigate GPU Load class."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: FrigateDataUpdateCoordinator,
        config_entry: ConfigEntry,
        gpu_name: str,
    ) -> None:
        """Construct a GpuLoadSensor."""
        self._gpu_name = gpu_name
        self._attr_name = f"{get_friendly_name(self._gpu_name)} gpu load"
        FrigateEntity.__init__(self, config_entry)
        CoordinatorEntity.__init__(self, coordinator)
        self._attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id, "gpu_load", self._gpu_name
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {get_frigate_device_identifier(self._config_entry)},
            "name": NAME,
            "model": self._get_model(),
            "configuration_url": self._config_entry.data.get(CONF_URL),
            "manufacturer": NAME,
        }

    @property
    def native_value(self) -> float | None:
        """Return the value of the sensor."""
        if self.coordinator.data:
            data = (
                self.coordinator.data.get("gpu_usages", {})
                .get(self._gpu_name, {})
                .get("gpu")
            )

            if data is None or not isinstance(data, str):
                return None

            try:
                return float(data.replace("%", "").strip())
            except ValueError:
                pass

        return None

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the native unit of measurement of the sensor."""
        return PERCENTAGE

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return ICON_SPEEDOMETER


class CameraFpsSensor(
    FrigateEntity, CoordinatorEntity[FrigateDataUpdateCoordinator], SensorEntity
):
    """Frigate Camera Fps class."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: FrigateDataUpdateCoordinator,
        config_entry: ConfigEntry,
        cam_name: str,
        fps_type: str,
    ) -> None:
        """Construct a CameraFpsSensor."""
        FrigateEntity.__init__(self, config_entry)
        CoordinatorEntity.__init__(self, coordinator)
        self._cam_name = cam_name
        self._fps_type = fps_type
        self._attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sensor_fps",
            f"{self._cam_name}_{self._fps_type}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {
                get_frigate_device_identifier(self._config_entry, self._cam_name)
            },
            "via_device": get_frigate_device_identifier(self._config_entry),
            "name": get_friendly_name(self._cam_name),
            "model": self._get_model(),
            "configuration_url": f"{self._config_entry.data.get(CONF_URL)}/cameras/{self._cam_name}",
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"{self._fps_type} fps"

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the native unit of measurement of the sensor."""
        return FPS

    @property
    def native_value(self) -> int | None:
        """Return the value of the sensor."""
        if self.coordinator.data:
            data = (
                self.coordinator.data.get("cameras", {})
                .get(self._cam_name, {})
                .get(f"{self._fps_type}_fps")
            )
            if data is not None:
                try:
                    return round(float(data))
                except ValueError:
                    pass
        return None

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return ICON_SPEEDOMETER


class CameraSoundSensor(
    FrigateEntity, CoordinatorEntity[FrigateDataUpdateCoordinator], SensorEntity
):
    """Frigate Camera Sound Level class."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.SOUND_PRESSURE

    def __init__(
        self,
        coordinator: FrigateDataUpdateCoordinator,
        config_entry: ConfigEntry,
        cam_name: str,
    ) -> None:
        """Construct a CameraSoundSensor."""
        FrigateEntity.__init__(self, config_entry)
        CoordinatorEntity.__init__(self, coordinator)
        self._cam_name = cam_name
        self._attr_entity_registry_enabled_default = True

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sensor_sound_level",
            f"{self._cam_name}_dB",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {
                get_frigate_device_identifier(self._config_entry, self._cam_name)
            },
            "via_device": get_frigate_device_identifier(self._config_entry),
            "name": get_friendly_name(self._cam_name),
            "model": self._get_model(),
            "configuration_url": f"{self._config_entry.data.get(CONF_URL)}/cameras/{self._cam_name}",
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "sound level"

    @property
    def native_unit_of_measurement(self) -> Any:
        """Return the native unit of measurement of the sensor."""
        return UnitOfSoundPressure.DECIBEL

    @property
    def native_value(self) -> int | None:
        """Return the value of the sensor."""
        if self.coordinator.data:
            data = (
                self.coordinator.data.get("cameras", {})
                .get(self._cam_name, {})
                .get("audio_dBFS")
            )
            if data is not None:
                try:
                    return round(float(data))
                except ValueError:
                    pass
        return None

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return ICON_WAVEFORM


class FrigateObjectCountSensor(FrigateMQTTEntity, SensorEntity):
    """Frigate Motion Sensor class."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        cam_name: str,
        obj_name: str,
        enable_attribute_tracking: bool = True,
    ) -> None:
        """Construct a FrigateObjectCountSensor."""
        self._cam_name = cam_name
        self._obj_name = obj_name
        self._state = 0
        self._frigate_config = frigate_config
        self._icon = get_icon_from_type(self._obj_name)
        # Combined classification counts: value -> count
        # Holds counts for both attribute and sublabel classifications.
        self._attribute_counts: dict[str, int] = {}
        # Per-model tracking: {model_key: {object_id: classification_value}}
        # Allows an object to hold one classification per model simultaneously.
        self._tracked_object_by_model: dict[str, dict[str, str]] = {}

        # Find which classification models (attribute and sublabel) apply to
        # this object.  Only populate when attribute tracking is enabled.
        self._attribute_models: list[str] = []
        self._sublabel_models: list[str] = []
        if enable_attribute_tracking:
            attribute_models_map = get_attribute_classification_models_and_base_objects(frigate_config)
            for model_key, base_objects in attribute_models_map.items():
                if obj_name in base_objects:
                    self._attribute_models.append(model_key)

            sublabel_models_map = get_sublabel_classification_models_and_base_objects(frigate_config)
            for model_key, base_objects in sublabel_models_map.items():
                if obj_name in base_objects:
                    self._sublabel_models.append(model_key)

        has_classification_models = bool(self._attribute_models or self._sublabel_models)

        primary_topic = (
            f"{self._frigate_config['mqtt']['topic_prefix']}"
            f"/{self._cam_name}/{self._obj_name}"
        )

        mqtt_prefix = self._frigate_config["mqtt"]["topic_prefix"]
        topics: dict[str, Any] = {
            "state_topic": {
                "msg_callback": self._state_message_received,
                "qos": 0,
                "topic": primary_topic,
                "encoding": None,
            },
        }
        if has_classification_models:
            topics["attribute_topic"] = {
                "msg_callback": self._classification_message_received,
                "qos": 0,
                "topic": f"{mqtt_prefix}/tracked_object_update",
                "encoding": None,
            }
            topics["events_topic"] = {
                "msg_callback": self._event_message_received,
                "qos": 0,
                "topic": f"{mqtt_prefix}/events",
                "encoding": None,
            }

        super().__init__(
            config_entry,
            frigate_config,
            topics,
        )

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle a new received MQTT state message."""
        try:
            self._state = int(msg.payload)
            self.async_write_ha_state()
        except ValueError:
            pass

    def _update_classification(
        self, model_key: str, object_id: str, value: str
    ) -> bool:
        """Update per-model classification tracking.  Returns True if changed."""
        model_objects = self._tracked_object_by_model.setdefault(model_key, {})
        old_value = model_objects.get(object_id)
        if old_value == value:
            return False
        # Decrement the old count
        if old_value is not None and old_value in self._attribute_counts:
            self._attribute_counts[old_value] = max(
                0, self._attribute_counts[old_value] - 1
            )
            if self._attribute_counts[old_value] == 0:
                del self._attribute_counts[old_value]
        # Increment the new count
        model_objects[object_id] = value
        self._attribute_counts[value] = self._attribute_counts.get(value, 0) + 1
        return True

    @callback
    def _classification_message_received(self, msg: ReceiveMessage) -> None:
        """Handle classification messages from tracked_object_update topic."""
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            if data.get("type") != "classification":
                return

            if data.get("camera") != self._cam_name:
                return

            model_key = data.get("model")
            object_id = data.get("id")
            if not object_id:
                return

            value: str | None = None
            if model_key in self._attribute_models:
                value = data.get("attribute")
            elif model_key in self._sublabel_models:
                value = data.get("sub_label")

            if not value:
                return

            if self._update_classification(model_key, object_id, value):
                self.async_write_ha_state()

        except (ValueError, KeyError):
            pass

    @callback
    def _event_message_received(self, msg: ReceiveMessage) -> None:
        """Handle event lifecycle messages from frigate/events topic."""
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            after = data.get("after")
            if not after:
                return

            if after.get("camera") != self._cam_name:
                return

            if after.get("label") != self._obj_name:
                return

            object_id = after.get("id")
            if not object_id:
                return

            end_time = after.get("end_time")

            if end_time is not None:
                # Object ended – remove from all model tracking
                changed = False
                for model_objects in self._tracked_object_by_model.values():
                    if object_id in model_objects:
                        old_value = model_objects.pop(object_id)
                        if old_value in self._attribute_counts:
                            self._attribute_counts[old_value] = max(
                                0, self._attribute_counts[old_value] - 1
                            )
                            if self._attribute_counts[old_value] == 0:
                                del self._attribute_counts[old_value]
                        changed = True
                if changed:
                    self.async_write_ha_state()
            else:
                # Event is active – pick up any classification data in the event
                current_attributes = after.get("current_attributes", [])
                changed = False
                for attr_data in current_attributes:
                    if not isinstance(attr_data, dict):
                        continue
                    model_key = attr_data.get("model")
                    if model_key in self._attribute_models:
                        value = attr_data.get("attribute")
                    elif model_key in self._sublabel_models:
                        value = attr_data.get("sub_label")
                    else:
                        continue
                    if value and self._update_classification(model_key, object_id, value):
                        changed = True
                if changed:
                    self.async_write_ha_state()

        except (ValueError, KeyError):
            pass

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sensor_object_count",
            f"{self._cam_name}_{self._obj_name}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {
                get_frigate_device_identifier(self._config_entry, self._cam_name)
            },
            "via_device": get_frigate_device_identifier(self._config_entry),
            "name": get_friendly_name(self._cam_name),
            "model": self._get_model(),
            "configuration_url": f"{self._config_entry.data.get(CONF_URL)}/cameras/{self._cam_name if self._cam_name not in get_zones(self._frigate_config) else ''}",
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"{get_friendly_name(self._obj_name)} count"

    @property
    def native_value(self) -> int:
        """Return the value of the sensor."""
        return self._state

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the native unit of measurement of the sensor."""
        return "objects"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        return self._attribute_counts if self._attribute_counts else {}

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return self._icon


class FrigateActiveObjectCountSensor(FrigateMQTTEntity, SensorEntity):
    """Frigate Motion Sensor class."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        cam_name: str,
        obj_name: str,
    ) -> None:
        """Construct a FrigateObjectCountSensor."""
        self._cam_name = cam_name
        self._obj_name = obj_name
        self._state = 0
        self._frigate_config = frigate_config
        self._icon = get_icon_from_type(self._obj_name)

        super().__init__(
            config_entry,
            frigate_config,
            {
                "state_topic": {
                    "msg_callback": self._state_message_received,
                    "qos": 0,
                    "topic": (
                        f"{self._frigate_config['mqtt']['topic_prefix']}"
                        f"/{self._cam_name}/{self._obj_name}"
                        "/active"
                    ),
                    "encoding": None,
                },
            },
        )

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle a new received MQTT state message."""
        try:
            self._state = int(msg.payload)
            self.async_write_ha_state()
        except ValueError:
            pass

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sensor_active_object_count",
            f"{self._cam_name}_{self._obj_name}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {
                get_frigate_device_identifier(self._config_entry, self._cam_name)
            },
            "via_device": get_frigate_device_identifier(self._config_entry),
            "name": get_friendly_name(self._cam_name),
            "model": self._get_model(),
            "configuration_url": f"{self._config_entry.data.get(CONF_URL)}/cameras/{self._cam_name if self._cam_name not in get_zones(self._frigate_config) else ''}",
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"{get_friendly_name(self._obj_name)} active count".title()

    @property
    def native_value(self) -> int:
        """Return the value of the sensor."""
        return self._state

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the native unit of measurement of the sensor."""
        return "objects"

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return self._icon


class FrigateSublabelCountSensor(FrigateMQTTEntity, SensorEntity):
    """Frigate Sublabel Count Sensor class - counts objects with a specific sublabel."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    # Subclasses can override to handle "attribute" instead of "sub_label"
    _classification_field: str = "sub_label"
    _unique_id_type: str = "sensor_sublabel_count"

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        cam_name: str,
        obj_name: str,
        model_key: str,
        sublabel_class: str,
        actual_cam_name: str | None = None,
    ) -> None:
        """Construct a FrigateSublabelCountSensor."""
        self._cam_name = cam_name
        self._obj_name = obj_name
        self._model_key = model_key
        self._sublabel_class = sublabel_class
        self._state = 0
        self._frigate_config = frigate_config
        self._icon = get_icon_from_type(self._obj_name)
        # actual_cam_name is the real camera name when cam_name is a zone
        self._actual_cam_name = actual_cam_name if actual_cam_name is not None else cam_name
        self._is_zone = cam_name != self._actual_cam_name
        # Track object_id -> classification value mapping.
        # For zone sensors this spans the parent camera; zone membership is
        # checked separately via _objects_in_zone.
        self._tracked_objects: dict[str, str] = {}
        # For zone sensors: set of object IDs currently inside this zone
        self._objects_in_zone: set[str] = set()

        mqtt_prefix = self._frigate_config["mqtt"]["topic_prefix"]
        super().__init__(
            config_entry,
            frigate_config,
            {
                "state_topic": {
                    "msg_callback": self._state_message_received,
                    "qos": 0,
                    "topic": f"{mqtt_prefix}/tracked_object_update",
                    "encoding": None,
                },
                "events_topic": {
                    "msg_callback": self._event_message_received,
                    "qos": 0,
                    "topic": f"{mqtt_prefix}/events",
                    "encoding": None,
                },
            },
        )

    def _recalculate_state(self) -> None:
        """Recalculate and update the sensor count."""
        if self._is_zone:
            self._state = sum(
                1
                for oid, val in self._tracked_objects.items()
                if val == self._sublabel_class and oid in self._objects_in_zone
            )
        else:
            self._state = sum(
                1 for val in self._tracked_objects.values() if val == self._sublabel_class
            )

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle classification messages from tracked_object_update topic."""
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            if data.get("type") != "classification":
                return

            # Filter by actual camera name (handles zone sensors correctly)
            if data.get("camera") != self._actual_cam_name:
                return

            if data.get("model") != self._model_key:
                return

            # Get the classification value (sub_label or attribute)
            value = data.get(self._classification_field)
            if not value:
                return

            object_id = data.get("id")
            if not object_id:
                return

            # For zone sensors without cached zone data, fall back to
            # current_zones in the classification message (if present)
            if self._is_zone and object_id not in self._objects_in_zone:
                current_zones = data.get("current_zones")
                if current_zones is not None:
                    if self._cam_name in current_zones:
                        self._objects_in_zone.add(object_id)
                    else:
                        return

            self._tracked_objects[object_id] = value
            self._recalculate_state()
            self.async_write_ha_state()

        except (ValueError, KeyError):
            pass

    @callback
    def _event_message_received(self, msg: ReceiveMessage) -> None:
        """Handle event lifecycle messages from frigate/events topic."""
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            after = data.get("after")
            if not after:
                return

            # Filter by actual camera name (handles zone sensors correctly)
            if after.get("camera") != self._actual_cam_name:
                return

            if after.get("label") != self._obj_name:
                return

            object_id = after.get("id")
            if not object_id:
                return

            end_time = after.get("end_time")

            if end_time is not None:
                # Object ended – remove from all tracking
                self._tracked_objects.pop(object_id, None)
                self._objects_in_zone.discard(object_id)
                self._recalculate_state()
                self.async_write_ha_state()
            else:
                # Object is active – update zone membership and check current_attributes
                if self._is_zone:
                    current_zones = after.get("current_zones", [])
                    if self._cam_name in current_zones:
                        self._objects_in_zone.add(object_id)
                    else:
                        self._objects_in_zone.discard(object_id)

                # Also pick up classification data that may be in the event
                current_attributes = after.get("current_attributes", [])
                for attr_data in current_attributes:
                    if isinstance(attr_data, dict):
                        if attr_data.get("model") == self._model_key:
                            value = attr_data.get(self._classification_field)
                            if value:
                                self._tracked_objects[object_id] = value
                                self._recalculate_state()
                                self.async_write_ha_state()
                                break

        except (ValueError, KeyError):
            pass

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            self._unique_id_type,
            f"{self._cam_name}_{self._obj_name}_{self._model_key}_{self._sublabel_class}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {
                get_frigate_device_identifier(self._config_entry, self._cam_name)
            },
            "via_device": get_frigate_device_identifier(self._config_entry),
            "name": get_friendly_name(self._cam_name),
            "model": self._get_model(),
            "configuration_url": f"{self._config_entry.data.get(CONF_URL)}/cameras/{self._cam_name if self._cam_name not in get_zones(self._frigate_config) else ''}",
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"{get_friendly_name(self._sublabel_class)} count"

    @property
    def native_value(self) -> int:
        """Return the value of the sensor."""
        return self._state

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the native unit of measurement of the sensor."""
        return "objects"

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return self._icon


class FrigateAttributeCountSensor(FrigateSublabelCountSensor):
    """Frigate Attribute Count Sensor class - counts objects with a specific attribute.

    This is a thin subclass of FrigateSublabelCountSensor that reads the
    ``attribute`` field from classification messages instead of ``sub_label``,
    and uses a distinct unique-ID type so attribute count entities are stored
    separately from sublabel count entities in the entity registry.
    """

    _classification_field: str = "attribute"
    _unique_id_type: str = "sensor_attribute_count"


class DeviceTempSensor(
    FrigateEntity, CoordinatorEntity[FrigateDataUpdateCoordinator], SensorEntity
):
    """Frigate Coral Temperature Sensor class."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.TEMPERATURE

    def __init__(
        self,
        coordinator: FrigateDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
    ) -> None:
        """Construct a CoralTempSensor."""
        self._name = name
        FrigateEntity.__init__(self, config_entry)
        CoordinatorEntity.__init__(self, coordinator)
        self._attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id, "sensor_temp", self._name
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {get_frigate_device_identifier(self._config_entry)},
            "name": NAME,
            "model": self._get_model(),
            "configuration_url": self._config_entry.data.get(CONF_URL),
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"{get_friendly_name(self._name)} temperature"

    @property
    def native_value(self) -> float | None:
        """Return the value of the sensor."""
        if self.coordinator.data:
            data = (
                self.coordinator.data.get("service", {})
                .get("temperatures", {})
                .get(self._name, 0.0)
            )
            try:
                return float(data)
            except (TypeError, ValueError):
                pass
        return None

    @property
    def native_unit_of_measurement(self) -> Any:
        """Return the native unit of measurement of the sensor."""
        return UnitOfTemperature.CELSIUS

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return ICON_CORAL


class CameraProcessCpuSensor(
    FrigateEntity, CoordinatorEntity[FrigateDataUpdateCoordinator], SensorEntity
):
    """Cpu usage for camera processes class."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: FrigateDataUpdateCoordinator,
        config_entry: ConfigEntry,
        cam_name: str,
        process_type: str,
    ) -> None:
        """Construct a CoralTempSensor."""
        self._cam_name = cam_name
        self._process_type = process_type
        self._attr_name = f"{self._process_type} cpu usage"
        FrigateEntity.__init__(self, config_entry)
        CoordinatorEntity.__init__(self, coordinator)
        self._attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            f"{self._process_type}_cpu_usage",
            self._cam_name,
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {
                get_frigate_device_identifier(self._config_entry, self._cam_name)
            },
            "via_device": get_frigate_device_identifier(self._config_entry),
            "name": get_friendly_name(self._cam_name),
            "model": self._get_model(),
            "configuration_url": f"{self._config_entry.data.get(CONF_URL)}/cameras/{self._cam_name}",
            "manufacturer": NAME,
        }

    @property
    def native_value(self) -> float | None:
        """Return the value of the sensor."""
        if self.coordinator.data:
            pid_key = (
                "pid" if self._process_type == "detect" else f"{self._process_type}_pid"
            )

            pid = str(
                self.coordinator.data.get("cameras", {})
                .get(self._cam_name, {})
                .get(pid_key, "-1")
            )

            data = (
                self.coordinator.data.get("cpu_usages", {})
                .get(pid, {})
                .get("cpu", None)
            )

            try:
                return float(data)
            except (TypeError, ValueError):
                pass
        return None

    @property
    def native_unit_of_measurement(self) -> Any:
        """Return the native unit of measurement of the sensor."""
        return PERCENTAGE

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return ICON_CORAL


class FrigateRecognizedFaceSensor(FrigateMQTTEntity, SensorEntity):
    """Frigate Recognized Face Sensor class."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        cam_name: str,
    ) -> None:
        """Construct a FrigateRecognizedFaceSensor."""
        self._cam_name = cam_name
        self._state = "Unknown"
        self._frigate_config = frigate_config
        self._clear_state_callable: Callable | None = None

        super().__init__(
            config_entry,
            frigate_config,
            {
                "state_topic": {
                    "msg_callback": self._state_message_received,
                    "qos": 0,
                    "topic": (
                        f"{self._frigate_config['mqtt']['topic_prefix']}"
                        "/tracked_object_update"
                    ),
                    "encoding": None,
                },
            },
        )

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle a new received MQTT state message."""
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            if data.get("type") != "face":
                return

            if data.get("camera") != self._cam_name:
                return

            self._state = data["name"]
            self.async_write_ha_state()

            if self._clear_state_callable:
                self._clear_state_callable()
                self._clear_state_callable = None

            self._clear_state_callable = async_call_later(
                self.hass, datetime.timedelta(seconds=60), self.clear_recognized_face
            )

        except ValueError:
            pass

    @callback
    def clear_recognized_face(self, _now: datetime.datetime) -> None:
        """Clears the current sensor state."""
        self._state = "None"
        self.async_write_ha_state()
        self._clear_state_callable = None

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sensor_recognized_face",
            f"{self._cam_name}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {
                get_frigate_device_identifier(self._config_entry, self._cam_name)
            },
            "via_device": get_frigate_device_identifier(self._config_entry),
            "name": get_friendly_name(self._cam_name),
            "model": self._get_model(),
            "configuration_url": f"{self._config_entry.data.get(CONF_URL)}/cameras/{self._cam_name if self._cam_name not in get_zones(self._frigate_config) else ''}",
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "Last Recognized Face"

    @property
    def native_value(self) -> str:
        """Return the value of the sensor."""
        return str(self._state).title()

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return ICON_FACE


class FrigateRecognizedPlateSensor(FrigateMQTTEntity, SensorEntity):
    """Frigate Recognized License Plate Sensor class."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        cam_name: str,
    ) -> None:
        """Construct a FrigateRecognizedPlateSensor."""
        self._cam_name = cam_name
        self._state = "Unknown"
        self._frigate_config = frigate_config
        self._clear_state_callable: Callable | None = None

        super().__init__(
            config_entry,
            frigate_config,
            {
                "state_topic": {
                    "msg_callback": self._state_message_received,
                    "qos": 0,
                    "topic": (
                        f"{self._frigate_config['mqtt']['topic_prefix']}"
                        "/tracked_object_update"
                    ),
                    "encoding": None,
                },
            },
        )

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle a new received MQTT state message."""
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            if data.get("type") != "lpr":
                return

            if data.get("camera") != self._cam_name:
                return

            if data.get("name"):
                self._state = str(data["name"]).title()
            else:
                self._state = str(data["plate"])

            self.async_write_ha_state()

            if self._clear_state_callable:
                self._clear_state_callable()
                self._clear_state_callable = None

            self._clear_state_callable = async_call_later(
                self.hass, datetime.timedelta(seconds=60), self.clear_recognized_plate
            )
        except ValueError:
            pass

    @callback
    def clear_recognized_plate(self, _now: datetime.datetime) -> None:
        """Clears the current sensor state."""
        self._state = "None"
        self.async_write_ha_state()
        self._clear_state_callable = None

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sensor_recognized_plate",
            f"{self._cam_name}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {
                get_frigate_device_identifier(self._config_entry, self._cam_name)
            },
            "via_device": get_frigate_device_identifier(self._config_entry),
            "name": get_friendly_name(self._cam_name),
            "model": self._get_model(),
            "configuration_url": f"{self._config_entry.data.get(CONF_URL)}/cameras/{self._cam_name if self._cam_name not in get_zones(self._frigate_config) else ''}",
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "Last Recognized Plate"

    @property
    def native_value(self) -> str:
        """Return the value of the sensor."""
        return self._state

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return ICON_LICENSE_PLATE


class FrigateClassificationSensor(FrigateMQTTEntity, RestoreSensor):
    """Frigate Classification Sensor class."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        cam_name: str,
        model_key: str,
    ) -> None:
        """Construct a FrigateClassificationSensor."""
        self._cam_name = cam_name
        self._model_key = model_key
        self._state = "Unknown"
        self._frigate_config = frigate_config

        super().__init__(
            config_entry,
            frigate_config,
            {
                "state_topic": {
                    "msg_callback": self._state_message_received,
                    "qos": 0,
                    "topic": (
                        f"{self._frigate_config['mqtt']['topic_prefix']}"
                        f"/{self._cam_name}/classification/{self._model_key}"
                    ),
                    "encoding": None,
                },
            },
        )

    async def async_added_to_hass(self) -> None:
        """Restore state when added to hass."""
        await super().async_added_to_hass()
        if (
            last_sensor_data := await self.async_get_last_sensor_data()
        ) and last_sensor_data.native_value:
            # Only restore if state is not "unknown", "Unknown", or "unavailable"
            native_value = str(last_sensor_data.native_value)
            if native_value.lower() not in ("unknown", "unavailable"):
                self._state = native_value
                self.async_write_ha_state()

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle a new received MQTT state message."""
        payload = (
            msg.payload.decode("utf-8")
            if isinstance(msg.payload, bytes)
            else str(msg.payload)
        )

        if payload:
            self._state = payload
            self.async_write_ha_state()

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sensor_classification",
            f"{self._cam_name}_{self._model_key}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {
                get_frigate_device_identifier(self._config_entry, self._cam_name)
            },
            "via_device": get_frigate_device_identifier(self._config_entry),
            "name": get_friendly_name(self._cam_name),
            "model": self._get_model(),
            "configuration_url": f"{self._config_entry.data.get(CONF_URL)}/cameras/{self._cam_name}",
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"{get_friendly_name(self._model_key)} Classification"

    @property
    def native_value(self) -> str:
        """Return the value of the sensor."""
        return self._state

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return "mdi:tag-text"


class FrigateObjectClassificationSensor(FrigateMQTTEntity, SensorEntity):
    """Frigate Object Classification Sensor class."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        cam_or_zone_name: str,
        model_key: str,
        actual_cam_name: str | None = None,
    ) -> None:
        """Construct a FrigateObjectClassificationSensor."""
        self._cam_or_zone_name = cam_or_zone_name
        self._model_key = model_key
        self._state: str | None = None
        self._frigate_config = frigate_config
        # If actual_cam_name is provided, this is a zone sensor and we need to filter by camera
        # For backward compatibility, if actual_cam_name is None, use cam_or_zone_name
        self._actual_cam_name = actual_cam_name if actual_cam_name is not None else cam_or_zone_name
        self._is_zone = self._cam_or_zone_name != self._actual_cam_name
        # Track object IDs that have been classified so we can clear state on lifecycle end
        self._classified_objects: set[str] = set()
        # For zone sensors, track which objects are currently in the zone
        self._objects_in_zone: set[str] = set()

        mqtt_prefix = self._frigate_config['mqtt']['topic_prefix']
        
        super().__init__(
            config_entry,
            frigate_config,
            {
                "state_topic": {
                    "msg_callback": self._state_message_received,
                    "qos": 0,
                    "topic": f"{mqtt_prefix}/tracked_object_update",
                    "encoding": None,
                },
                "events_topic": {
                    "msg_callback": self._event_message_received,
                    "qos": 0,
                    "topic": f"{mqtt_prefix}/events",
                    "encoding": None,
                },
            },
        )

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle a new received MQTT state message."""
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            if data.get("type") != "classification":
                return

            if data.get("camera") != self._actual_cam_name:
                return

            if data.get("model") != self._model_key:
                return

            # For zone sensors, check if the object is currently in the zone.
            # Use cached zone membership if available, otherwise fall back to
            # current_zones in the classification message.
            object_id = data.get("id")
            if self._is_zone:
                if object_id and object_id in self._classified_objects:
                    # We already know about this object; use zone cache
                    if object_id not in self._objects_in_zone:
                        return
                else:
                    # Fall back to current_zones field in classification message
                    current_zones = data.get("current_zones", [])
                    if self._cam_or_zone_name not in current_zones:
                        return

            # Extract sub_label or attribute from the payload
            if "sub_label" in data:
                self._state = str(data["sub_label"]).replace("_", " ").title()
            elif "attribute" in data:
                self._state = str(data["attribute"]).replace("_", " ").title()
            else:
                return

            if object_id:
                self._classified_objects.add(object_id)

            self.async_write_ha_state()

        except (ValueError, KeyError):
            pass

    @callback
    def _event_message_received(self, msg: ReceiveMessage) -> None:
        """Handle event lifecycle messages from frigate/events topic."""
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            # Event data is in the 'after' key
            after = data.get("after")
            if not after:
                return

            # Only process events for this camera
            if after.get("camera") != self._actual_cam_name:
                return

            # Determine if this is a zone sensor
            is_zone_sensor = self._cam_or_zone_name != self._actual_cam_name
            
            # For zone sensors, check if the object is in the zone
            if is_zone_sensor:
                current_zones = after.get("current_zones", [])
                if self._cam_or_zone_name not in current_zones:
                    return

            # Look for classification data in current_attributes
            current_attributes = after.get("current_attributes", [])
            
            for attr_data in current_attributes:
                if not isinstance(attr_data, dict):
                    continue
                    
                model_key = attr_data.get("model")
                if model_key != self._model_key:
                    continue

                # Extract sub_label or attribute from the classification data
                if "sub_label" in attr_data:
                    self._state = str(attr_data["sub_label"]).replace("_", " ").title()
                elif "attribute" in attr_data:
                    self._state = str(attr_data["attribute"]).replace("_", " ").title()
                else:
                    continue

                self.async_write_ha_state()

                if self._clear_state_callable:
                    self._clear_state_callable()
                    self._clear_state_callable = None

                self._clear_state_callable = async_call_later(
                    self.hass,
                    datetime.timedelta(seconds=60),
                    self.clear_classification,
                )
                break

        except (ValueError, KeyError):
            pass

    @callback
    def clear_classification(self, _now: datetime.datetime) -> None:
        """Clears the current sensor state."""
        self._state = "None"
        self.async_write_ha_state()
        self._clear_state_callable = None

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sensor_object_classification",
            f"{self._cam_or_zone_name}_{self._model_key}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        # Zones don't have a camera configuration page
        cam_suffix = "" if self._cam_or_zone_name in get_zones(self._frigate_config) else self._cam_or_zone_name
        return {
            "identifiers": {
                get_frigate_device_identifier(self._config_entry, self._cam_or_zone_name)
            },
            "via_device": get_frigate_device_identifier(self._config_entry),
            "name": get_friendly_name(self._cam_or_zone_name),
            "model": self._get_model(),
            "configuration_url": f"{self._config_entry.data.get(CONF_URL)}/cameras/{cam_suffix}",
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"{get_friendly_name(self._model_key)} Object Classification"

    @property
    def native_value(self) -> str | None:
        """Return the value of the sensor."""
        return self._state

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return "mdi:tag-text"


class FrigateReviewStatusSensor(FrigateMQTTEntity, SensorEntity):
    """Frigate Review Status Sensor class."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        cam_name: str,
    ) -> None:
        """Construct a FrigateReviewStatusSensor."""
        self._cam_name = cam_name
        self._state: str | None = None
        self._frigate_config = frigate_config

        super().__init__(
            config_entry,
            frigate_config,
            {
                "state_topic": {
                    "msg_callback": self._state_message_received,
                    "qos": 0,
                    "topic": (
                        f"{self._frigate_config['mqtt']['topic_prefix']}"
                        f"/{self._cam_name}/review_status"
                    ),
                    "encoding": None,
                },
            },
        )

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle a new received MQTT state message."""
        payload = (
            msg.payload.decode("utf-8")
            if isinstance(msg.payload, bytes)
            else str(msg.payload)
        )

        self._state = payload
        self.async_write_ha_state()

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sensor_review_status",
            f"{self._cam_name}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {
                get_frigate_device_identifier(self._config_entry, self._cam_name)
            },
            "via_device": get_frigate_device_identifier(self._config_entry),
            "name": get_friendly_name(self._cam_name),
            "model": self._get_model(),
            "configuration_url": f"{self._config_entry.data.get(CONF_URL)}/cameras/{self._cam_name}",
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "Review Status"

    @property
    def native_value(self) -> str | None:
        """Return the value of the sensor."""
        return self._state

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return "mdi:eye-check"


class FrigateGlobalFaceSensor(FrigateMQTTEntity, SensorEntity):
    """Frigate Global Face Sensor class - shows last camera where face was detected."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        face_name: str,
    ) -> None:
        """Construct a FrigateGlobalFaceSensor."""
        self._face_name = face_name
        self._state = "Unknown"
        self._frigate_config = frigate_config

        super().__init__(
            config_entry,
            frigate_config,
            {
                "state_topic": {
                    "msg_callback": self._state_message_received,
                    "qos": 0,
                    "topic": (
                        f"{self._frigate_config['mqtt']['topic_prefix']}"
                        "/tracked_object_update"
                    ),
                    "encoding": None,
                },
            },
        )

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle a new received MQTT state message."""
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            if data.get("type") != "face":
                return

            if data.get("name") != self._face_name:
                return

            camera = data.get("camera")
            if camera:
                self._state = get_friendly_name(camera)
                self.async_write_ha_state()

        except (ValueError, KeyError):
            pass

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sensor_global_face",
            self._face_name,
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {get_frigate_device_identifier(self._config_entry)},
            "name": NAME,
            "model": self._get_model(),
            "configuration_url": self._config_entry.data.get(CONF_URL),
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"{get_friendly_name(self._face_name)} Last Camera"

    @property
    def native_value(self) -> str:
        """Return the value of the sensor."""
        return self._state

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return ICON_FACE


class FrigateGlobalPlateSensor(FrigateMQTTEntity, SensorEntity):
    """Frigate Global License Plate Sensor class - shows last camera where plate was detected."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        plate_name: str,
    ) -> None:
        """Construct a FrigateGlobalPlateSensor."""
        self._plate_name = plate_name
        self._state = "Unknown"
        self._frigate_config = frigate_config

        super().__init__(
            config_entry,
            frigate_config,
            {
                "state_topic": {
                    "msg_callback": self._state_message_received,
                    "qos": 0,
                    "topic": (
                        f"{self._frigate_config['mqtt']['topic_prefix']}"
                        "/tracked_object_update"
                    ),
                    "encoding": None,
                },
            },
        )

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle a new received MQTT state message."""
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            if data.get("type") != "lpr":
                return

            # Only check name - plate number only appears when not recognized
            plate_name = data.get("name")
            if not plate_name or plate_name != self._plate_name:
                return

            camera = data.get("camera")
            if camera:
                self._state = get_friendly_name(camera)
                self.async_write_ha_state()

        except (ValueError, KeyError):
            pass

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sensor_global_plate",
            self._plate_name,
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {get_frigate_device_identifier(self._config_entry)},
            "name": NAME,
            "model": self._get_model(),
            "configuration_url": self._config_entry.data.get(CONF_URL),
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"{get_friendly_name(self._plate_name)} Last Camera"

    @property
    def native_value(self) -> str:
        """Return the value of the sensor."""
        return self._state

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return ICON_LICENSE_PLATE


class FrigateGlobalObjectClassificationSensor(FrigateMQTTEntity, SensorEntity):
    """Frigate Global Object Classification Sensor class - shows last camera where classification was detected."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        model_key: str,
        class_name: str,
    ) -> None:
        """Construct a FrigateGlobalObjectClassificationSensor."""
        self._model_key = model_key
        self._class_name = class_name
        self._state = "Unknown"
        self._frigate_config = frigate_config

        super().__init__(
            config_entry,
            frigate_config,
            {
                "state_topic": {
                    "msg_callback": self._state_message_received,
                    "qos": 0,
                    "topic": (
                        f"{self._frigate_config['mqtt']['topic_prefix']}"
                        "/tracked_object_update"
                    ),
                    "encoding": None,
                },
            },
        )

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle a new received MQTT state message."""
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            if data.get("type") != "classification":
                return

            if data.get("model") != self._model_key:
                return

            detected_class = None

            if "sub_label" in data:
                detected_class = str(data["sub_label"])
            elif "attribute" in data:
                detected_class = str(data["attribute"])

            if not detected_class or detected_class != self._class_name:
                return

            camera = data.get("camera")
            if camera:
                self._state = get_friendly_name(camera)
                self.async_write_ha_state()

        except (ValueError, KeyError):
            pass

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sensor_global_object_classification",
            f"{self._model_key}_{self._class_name}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {get_frigate_device_identifier(self._config_entry)},
            "name": NAME,
            "model": self._get_model(),
            "configuration_url": self._config_entry.data.get(CONF_URL),
            "manufacturer": NAME,
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        formatted_class = get_friendly_name(self._class_name)
        return f"{formatted_class} Last Camera"

    @property
    def native_value(self) -> str:
        """Return the value of the sensor."""
        return self._state

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return "mdi:tag-text"

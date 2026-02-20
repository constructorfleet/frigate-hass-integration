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
    build_mqtt_topics_with_optional_tracking,
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


async def _create_sublabel_sensors(
    entry: ConfigEntry,
    frigate_config: dict[str, Any],
    client: Any,
    entities: list[FrigateEntity],
) -> None:
    """Create count sensors for sublabel classifications."""
    sublabel_models = get_sublabel_classification_models_and_base_objects(frigate_config)
    
    for model_key, base_objects in sublabel_models.items():
        try:
            # Get the sublabel classes from the API
            classes = await client.async_get_classification_model_classes(model_key)
            
            # For each sublabel class, create sensors for each camera/zone where the base object could appear
            for sublabel_class in classes:
                # Get all cameras and zones where the base object(s) are tracked
                for cam_name, obj_name in get_cameras_zones_and_objects(frigate_config):
                    # Only create sensors if this camera/zone tracks one of the base objects
                    if obj_name in base_objects:
                        # Create a count sensor for this sublabel on this camera/zone
                        # The sensor name will be like "Dog A Count" for camera "front_door"
                        entities.append(
                            FrigateSublabelCountSensor(
                                entry,
                                frigate_config,
                                cam_name,
                                obj_name,
                                model_key,
                                sublabel_class,
                            )
                        )
        except Exception:
            _LOGGER.warning(
                "Failed to fetch sublabel classes for model %s. "
                "Sublabel count sensors will not be created for this model.",
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
        # Track attribute classifications: attribute_name -> count
        self._attribute_counts: dict[str, int] = {}
        # Track object_id -> attribute mapping
        # Note: Entries persist for object lifecycle. The primary count from the
        # main MQTT topic remains authoritative. Attribute counts are supplementary.
        self._tracked_object_attributes: dict[str, str] = {}
        
        # Find which attribute classification models apply to this object
        # Only check if attribute tracking is enabled
        self._attribute_models = []
        if enable_attribute_tracking:
            attribute_models_map = get_attribute_classification_models_and_base_objects(frigate_config)
            for model_key, base_objects in attribute_models_map.items():
                if obj_name in base_objects:
                    self._attribute_models.append(model_key)

        primary_topic = (
            f"{self._frigate_config['mqtt']['topic_prefix']}"
            f"/{self._cam_name}/{self._obj_name}"
        )
        
        topics = build_mqtt_topics_with_optional_tracking(
            frigate_config,
            cam_name,
            obj_name,
            primary_topic,
            self._state_message_received,
            self._attribute_message_received if self._attribute_models else None,
            self._event_message_received if self._attribute_models else None,
        )

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
    
    @callback
    def _attribute_message_received(self, msg: ReceiveMessage) -> None:
        """Handle attribute classification messages from tracked_object_update topic.
        
        This provides redundancy with the events topic - objects can be added to
        tracking from either source to ensure nothing is missed.
        """
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            # Only process classification messages for this camera
            if data.get("type") != "classification":
                return

            if data.get("camera") != self._cam_name:
                return
            
            # Check if this is one of our attribute models
            model_key = data.get("model")
            if model_key not in self._attribute_models:
                return

            # Get the attribute from the message
            attribute = data.get("attribute")
            if not attribute:
                return

            # Get the object ID
            object_id = data.get("id")
            if not object_id:
                return

            # Update our tracking of this object's attribute
            old_attribute = self._tracked_object_attributes.get(object_id)
            
            # Decrement old attribute count
            if old_attribute and old_attribute in self._attribute_counts:
                self._attribute_counts[old_attribute] = max(
                    0, self._attribute_counts[old_attribute] - 1
                )
            
            # Update to new attribute
            self._tracked_object_attributes[object_id] = attribute
            
            # Increment new attribute count
            self._attribute_counts[attribute] = (
                self._attribute_counts.get(attribute, 0) + 1
            )
            
            self.async_write_ha_state()

        except (ValueError, KeyError):
            pass
    
    @callback
    def _event_message_received(self, msg: ReceiveMessage) -> None:
        """Handle event lifecycle messages from frigate/events topic.
        
        This provides redundancy with tracked_object_update - objects can be added
        from either source. However, only events can remove objects when they end.
        """
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            # Event data is in the 'after' key
            after = data.get("after")
            if not after:
                return

            # Only process events for this camera and object type
            if after.get("camera") != self._cam_name:
                return
            
            if after.get("label") != self._obj_name:
                return
            
            # Get the object ID
            object_id = after.get("id")
            if not object_id:
                return
            
            # Check if event has ended (end_time is not null)
            end_time = after.get("end_time")
            
            if end_time is not None:
                # Event ended - remove this object from our tracking
                if object_id in self._tracked_object_attributes:
                    old_attribute = self._tracked_object_attributes.pop(object_id)
                    
                    # Decrement the attribute count
                    if old_attribute in self._attribute_counts:
                        self._attribute_counts[old_attribute] = max(
                            0, self._attribute_counts[old_attribute] - 1
                        )
                    
                    self.async_write_ha_state()
            else:
                # Event is active - check if we have attribute data to track
                # The event may contain current_attributes with classification data
                current_attributes = after.get("current_attributes", [])
                
                # Look for attributes from our tracked models
                for attr_data in current_attributes:
                    if isinstance(attr_data, dict):
                        model_key = attr_data.get("model")
                        if model_key in self._attribute_models:
                            attribute = attr_data.get("attribute")
                            if attribute:
                                # Update tracking
                                old_attribute = self._tracked_object_attributes.get(object_id)
                                
                                # Decrement old attribute count
                                if old_attribute and old_attribute in self._attribute_counts:
                                    self._attribute_counts[old_attribute] = max(
                                        0, self._attribute_counts[old_attribute] - 1
                                    )
                                
                                # Update to new attribute
                                self._tracked_object_attributes[object_id] = attribute
                                
                                # Increment new attribute count
                                self._attribute_counts[attribute] = (
                                    self._attribute_counts.get(attribute, 0) + 1
                                )
                                
                                self.async_write_ha_state()
                                break

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
        if self._attribute_counts:
            return self._attribute_counts
        return {}

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

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        cam_name: str,
        obj_name: str,
        model_key: str,
        sublabel_class: str,
    ) -> None:
        """Construct a FrigateSublabelCountSensor."""
        self._cam_name = cam_name
        self._obj_name = obj_name
        self._model_key = model_key
        self._sublabel_class = sublabel_class
        self._state = 0
        self._frigate_config = frigate_config
        self._icon = get_icon_from_type(self._obj_name)
        # Track object_id -> sublabel mapping
        # Note: Entries persist for object lifecycle. Classification messages
        # are event-driven and objects may be reclassified over time.
        self._tracked_objects: dict[str, str] = {}

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
                "events_topic": {
                    "msg_callback": self._event_message_received,
                    "qos": 0,
                    "topic": (
                        f"{self._frigate_config['mqtt']['topic_prefix']}"
                        "/events"
                    ),
                    "encoding": None,
                },
            },
        )

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle classification messages from tracked_object_update topic.
        
        This provides redundancy with the events topic - objects can be added to
        tracking from either source to ensure nothing is missed.
        """
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            # Only process classification messages for this camera/model
            if data.get("type") != "classification":
                return

            if data.get("camera") != self._cam_name:
                return

            if data.get("model") != self._model_key:
                return

            # Get the sublabel from the message
            sublabel = data.get("sub_label")
            if not sublabel:
                return

            # Get the object ID
            object_id = data.get("id")
            if not object_id:
                return

            # Update our tracking of this object's sublabel
            self._tracked_objects[object_id] = sublabel

            # Count how many tracked objects have our specific sublabel
            self._state = sum(
                1 for sl in self._tracked_objects.values() if sl == self._sublabel_class
            )
            self.async_write_ha_state()

        except (ValueError, KeyError):
            pass
    
    @callback
    def _event_message_received(self, msg: ReceiveMessage) -> None:
        """Handle event lifecycle messages from frigate/events topic.
        
        This provides redundancy with tracked_object_update - objects can be added
        from either source. However, only events can remove objects when they end.
        """
        try:
            data: dict[str, Any] = json.loads(msg.payload)

            # Event data is in the 'after' key
            after = data.get("after")
            if not after:
                return

            # Only process events for this camera and object type
            if after.get("camera") != self._cam_name:
                return
            
            if after.get("label") != self._obj_name:
                return
            
            # Get the object ID
            object_id = after.get("id")
            if not object_id:
                return
            
            # Check if event has ended (end_time is not null)
            end_time = after.get("end_time")
            
            if end_time is not None:
                # Event ended - remove this object from our tracking
                if object_id in self._tracked_objects:
                    self._tracked_objects.pop(object_id)
                    
                    # Recalculate count
                    self._state = sum(
                        1 for sl in self._tracked_objects.values() if sl == self._sublabel_class
                    )
                    
                    self.async_write_ha_state()
            else:
                # Event is active - check if we have sublabel data to track
                # The event may contain current_attributes with classification data
                current_attributes = after.get("current_attributes", [])
                
                # Look for sublabels from our tracked model
                for attr_data in current_attributes:
                    if isinstance(attr_data, dict):
                        model_key = attr_data.get("model")
                        if model_key == self._model_key:
                            sublabel = attr_data.get("sub_label")
                            if sublabel:
                                # Update our tracking of this object's sublabel
                                self._tracked_objects[object_id] = sublabel
                                
                                # Recalculate count
                                self._state = sum(
                                    1 for sl in self._tracked_objects.values() if sl == self._sublabel_class
                                )
                                
                                self.async_write_ha_state()
                                break

        except (ValueError, KeyError):
            pass

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sensor_sublabel_count",
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
        self._state = "Unknown"
        self._frigate_config = frigate_config
        self._clear_state_callable: Callable | None = None
        # If actual_cam_name is provided, this is a zone sensor and we need to filter by camera
        # For backward compatibility, if actual_cam_name is None, use cam_or_zone_name
        self._actual_cam_name = actual_cam_name if actual_cam_name is not None else cam_or_zone_name

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

            # For zone sensors, also check if the object is in the zone
            if self._cam_or_zone_name != self._actual_cam_name:
                # This is a zone sensor, check if object is in the zone
                current_zones = data.get("current_zones", [])
                if self._cam_or_zone_name not in current_zones:
                    return

            if data.get("model") != self._model_key:
                return

            # Extract sub_label or attribute from the payload
            if "sub_label" in data:
                self._state = str(data["sub_label"]).replace("_", " ").title()
            elif "attribute" in data:
                self._state = str(data["attribute"]).replace("_", " ").title()
            else:
                return

            self.async_write_ha_state()

            if self._clear_state_callable:
                self._clear_state_callable()
                self._clear_state_callable = None

            self._clear_state_callable = async_call_later(
                self.hass,
                datetime.timedelta(seconds=60),
                self.clear_classification,
            )

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

            # For zone sensors, check if the object is in the zone
            if self._cam_or_zone_name != self._actual_cam_name:
                # This is a zone sensor, check if object is in the zone
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
    def native_value(self) -> str:
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

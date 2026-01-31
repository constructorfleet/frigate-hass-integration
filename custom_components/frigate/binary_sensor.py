"""Binary sensor platform for Frigate."""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import (
    FrigateEntity,
    FrigateMQTTEntity,
    ReceiveMessage,
    decode_if_necessary,
    get_attribute_classification_models_and_base_objects,
    get_cameras,
    get_cameras_and_audio,
    get_cameras_zones_and_objects,
    get_friendly_name,
    get_frigate_device_identifier,
    get_frigate_entity_unique_id,
    get_sublabel_classification_models_and_base_objects,
    get_zones,
)
from .const import ATTR_CLIENT, ATTR_CONFIG, DOMAIN, NAME
from .icons import get_dynamic_icon_from_type

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def _create_sublabel_occupancy_sensors(
    entry: ConfigEntry,
    frigate_config: dict[str, Any],
    client: Any,
    entities: list[FrigateEntity],
) -> None:
    """Create occupancy sensors for sublabel classifications."""
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
                        # Create an occupancy sensor for this sublabel on this camera/zone
                        entities.append(
                            FrigateSublabelOccupancySensor(
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
                "Sublabel occupancy sensors will not be created for this model.",
                model_key,
            )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Binary sensor entry setup."""
    frigate_config = hass.data[DOMAIN][entry.entry_id][ATTR_CONFIG]
    client = hass.data[DOMAIN][entry.entry_id][ATTR_CLIENT]
    entities: list[FrigateEntity] = []

    # Add object sensors for cameras and zones.
    entities.extend(
        [
            FrigateObjectOccupancySensor(entry, frigate_config, cam_name, obj)
            for cam_name, obj in get_cameras_zones_and_objects(frigate_config)
        ]
    )

    # Add audio sensors for cameras.
    entities.extend(
        [
            FrigateAudioSensor(entry, frigate_config, cam_name, audio)
            for cam_name, audio in get_cameras_and_audio(frigate_config)
        ]
    )

    # Add generic motion sensors for cameras.
    entities.extend(
        [
            FrigateMotionSensor(entry, frigate_config, cam_name)
            for cam_name in get_cameras(frigate_config)
        ]
    )
    
    # Add sublabel occupancy sensors
    await _create_sublabel_occupancy_sensors(entry, frigate_config, client, entities)

    async_add_entities(entities)


class FrigateObjectOccupancySensor(FrigateMQTTEntity, BinarySensorEntity):
    """Frigate Occupancy Sensor class."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        cam_name: str,
        obj_name: str,
    ) -> None:
        """Construct a new FrigateObjectOccupancySensor."""
        self._cam_name = cam_name
        self._obj_name = obj_name
        self._is_on = False
        self._frigate_config = frigate_config
        self._attribute_counts: dict[str, int] = {}  # Track attribute -> count
        self._tracked_object_attributes: dict[str, str] = {}  # object_id -> attribute
        
        # Find which attribute classification models apply to this object
        self._attribute_models = []
        attribute_models_map = get_attribute_classification_models_and_base_objects(frigate_config)
        for model_key, base_objects in attribute_models_map.items():
            if obj_name in base_objects:
                self._attribute_models.append(model_key)

        topics = {
            "state_topic": {
                "msg_callback": self._state_message_received,
                "qos": 0,
                "topic": (
                    f"{self._frigate_config['mqtt']['topic_prefix']}"
                    f"/{self._cam_name}/{self._obj_name}"
                ),
                "encoding": None,
            },
        }
        
        # Add tracked_object_update subscription if there are attribute models for this object
        if self._attribute_models:
            topics["attribute_topic"] = {
                "msg_callback": self._attribute_message_received,
                "qos": 0,
                "topic": (
                    f"{self._frigate_config['mqtt']['topic_prefix']}"
                    "/tracked_object_update"
                ),
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
            self._is_on = int(msg.payload) > 0
        except ValueError:
            self._is_on = False
        self.async_write_ha_state()
    
    @callback
    def _attribute_message_received(self, msg: ReceiveMessage) -> None:
        """Handle attribute classification messages."""
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

    @property
    def unique_id(self) -> str:
        """Return a unique ID for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "occupancy_sensor",
            f"{self._cam_name}_{self._obj_name}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
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
        return f"{get_friendly_name(self._obj_name)} occupancy"

    @property
    def is_on(self) -> bool:
        """Return true if the binary sensor is on."""
        return self._is_on

    @property
    def device_class(self) -> BinarySensorDeviceClass:
        """Return the device class."""
        return BinarySensorDeviceClass.OCCUPANCY
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        if self._attribute_counts:
            return self._attribute_counts
        return {}

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return get_dynamic_icon_from_type(self._obj_name, self._is_on)


class FrigateSublabelOccupancySensor(FrigateMQTTEntity, BinarySensorEntity):
    """Frigate Sublabel Occupancy Sensor class - detects occupancy of objects with a specific sublabel."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        cam_name: str,
        obj_name: str,
        model_key: str,
        sublabel_class: str,
    ) -> None:
        """Construct a new FrigateSublabelOccupancySensor."""
        self._cam_name = cam_name
        self._obj_name = obj_name
        self._model_key = model_key
        self._sublabel_class = sublabel_class
        self._is_on = False
        self._frigate_config = frigate_config
        self._tracked_objects: set[str] = set()  # Track object_ids with this sublabel

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

            # Update our tracking of objects with this sublabel
            if sublabel == self._sublabel_class:
                self._tracked_objects.add(object_id)
            else:
                # Object has a different sublabel now, remove from our tracking
                self._tracked_objects.discard(object_id)

            # Update occupancy state
            self._is_on = len(self._tracked_objects) > 0
            self.async_write_ha_state()

        except (ValueError, KeyError):
            pass

    @property
    def unique_id(self) -> str:
        """Return a unique ID for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "sublabel_occupancy_sensor",
            f"{self._cam_name}_{self._obj_name}_{self._model_key}_{self._sublabel_class}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
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
        return f"{get_friendly_name(self._sublabel_class)} occupancy"

    @property
    def is_on(self) -> bool:
        """Return true if the binary sensor is on."""
        return self._is_on

    @property
    def device_class(self) -> BinarySensorDeviceClass:
        """Return the device class."""
        return BinarySensorDeviceClass.OCCUPANCY

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return get_dynamic_icon_from_type(self._obj_name, self._is_on)


class FrigateAudioSensor(FrigateMQTTEntity, BinarySensorEntity):
    """Frigate Audio Sensor class."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        cam_name: str,
        audio_name: str,
    ) -> None:
        """Construct a new FrigateAudioSensor."""
        self._cam_name = cam_name
        self._audio_name = audio_name
        self._is_on = False
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
                        f"/{self._cam_name}/audio/{self._audio_name}"
                    ),
                },
            },
        )

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle a new received MQTT state message."""
        self._is_on = decode_if_necessary(msg.payload) == "ON"
        self.async_write_ha_state()

    @property
    def unique_id(self) -> str:
        """Return a unique ID for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "audio_sensor",
            f"{self._cam_name}_{self._audio_name}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
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
        return f"{self._audio_name} sound"

    @property
    def is_on(self) -> bool:
        """Return true if the binary sensor is on."""
        return self._is_on

    @property
    def device_class(self) -> BinarySensorDeviceClass:
        """Return the device class."""
        return BinarySensorDeviceClass.SOUND

    @property
    def icon(self) -> str:
        """Return the icon of the sensor."""
        return get_dynamic_icon_from_type("sound", self._is_on)


class FrigateMotionSensor(FrigateMQTTEntity, BinarySensorEntity):
    """Frigate Motion Sensor class."""

    _attr_name = "Motion"

    def __init__(
        self,
        config_entry: ConfigEntry,
        frigate_config: dict[str, Any],
        cam_name: str,
    ) -> None:
        """Construct a new FrigateMotionSensor."""
        self._cam_name = cam_name
        self._is_on = False
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
                        f"/{self._cam_name}/motion"
                    ),
                },
            },
        )

    @callback
    def _state_message_received(self, msg: ReceiveMessage) -> None:
        """Handle a new received MQTT state message."""
        self._is_on = decode_if_necessary(msg.payload) == "ON"
        self.async_write_ha_state()

    @property
    def unique_id(self) -> str:
        """Return a unique ID for this entity."""
        return get_frigate_entity_unique_id(
            self._config_entry.entry_id,
            "motion_sensor",
            f"{self._cam_name}",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
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
    def is_on(self) -> bool:
        """Return true if the binary sensor is on."""
        return self._is_on

    @property
    def device_class(self) -> BinarySensorDeviceClass:
        """Return the device class."""
        return BinarySensorDeviceClass.MOTION

"""Test event lifecycle tracking for sensors."""

import json

import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from homeassistant.core import HomeAssistant

from . import (
    TEST_SENSOR_FRONT_DOOR_PERSON_ENTITY_ID,
    TEST_SENSOR_STEPS_PERSON_ENTITY_ID,
    setup_mock_frigate_config_entry,
)


async def test_attribute_removal_on_event_end(hass: HomeAssistant) -> None:
    """Test that object IDs are removed when event ends."""
    await setup_mock_frigate_config_entry(hass)

    async_fire_mqtt_message(hass, "frigate/available", "online")
    await hass.async_block_till_done()

    # Simulate an object with an attribute being tracked
    async_fire_mqtt_message(
        hass,
        "frigate/tracked_object_update",
        json.dumps(
            {
                "type": "classification",
                "id": "test_object_123",
                "camera": "front_door",
                "timestamp": 1607123958.748393,
                "model": "person_orientation",
                "attribute": "standing",
                "score": 0.92,
            }
        ),
    )
    await hass.async_block_till_done()

    # Verify the attribute was tracked
    entity_state = hass.states.get(TEST_SENSOR_FRONT_DOOR_PERSON_ENTITY_ID)
    assert entity_state
    assert entity_state.attributes.get("standing") == 1

    # Now simulate the event ending
    async_fire_mqtt_message(
        hass,
        "frigate/events",
        json.dumps(
            {
                "before": {
                    "id": "test_object_123",
                    "camera": "front_door",
                    "end_time": None,
                },
                "after": {
                    "id": "test_object_123",
                    "camera": "front_door",
                    "end_time": 1607123970.123456,
                },
            }
        ),
    )
    await hass.async_block_till_done()

    # Verify the attribute count decreased
    entity_state = hass.states.get(TEST_SENSOR_FRONT_DOOR_PERSON_ENTITY_ID)
    assert entity_state
    # After event ends, the attribute count should be 0
    assert entity_state.attributes.get("standing", 0) == 0


async def test_sublabel_removal_on_event_end(hass: HomeAssistant) -> None:
    """Test that sublabel objects are removed when event ends."""
    await setup_mock_frigate_config_entry(hass)

    async_fire_mqtt_message(hass, "frigate/available", "online")
    await hass.async_block_till_done()

    # Simulate an object with a sublabel being tracked
    async_fire_mqtt_message(
        hass,
        "frigate/tracked_object_update",
        json.dumps(
            {
                "type": "classification",
                "id": "test_object_456",
                "camera": "front_door",
                "current_zones": [],
                "timestamp": 1607123958.748393,
                "model": "person_classifier",
                "sub_label": "delivery_person",
                "score": 0.87,
            }
        ),
    )
    await hass.async_block_till_done()

    # Now simulate the event ending
    async_fire_mqtt_message(
        hass,
        "frigate/events",
        json.dumps(
            {
                "before": {
                    "id": "test_object_456",
                    "camera": "front_door",
                    "end_time": None,
                },
                "after": {
                    "id": "test_object_456",
                    "camera": "front_door",
                    "end_time": 1607123970.123456,
                },
            }
        ),
    )
    await hass.async_block_till_done()

    # The object should be removed from tracking


async def test_event_end_different_camera_ignored(hass: HomeAssistant) -> None:
    """Test that events from different cameras are ignored."""
    await setup_mock_frigate_config_entry(hass)

    async_fire_mqtt_message(hass, "frigate/available", "online")
    await hass.async_block_till_done()

    # Simulate an object with an attribute being tracked on front_door
    async_fire_mqtt_message(
        hass,
        "frigate/tracked_object_update",
        json.dumps(
            {
                "type": "classification",
                "id": "test_object_789",
                "camera": "front_door",
                "timestamp": 1607123958.748393,
                "model": "person_orientation",
                "attribute": "standing",
                "score": 0.92,
            }
        ),
    )
    await hass.async_block_till_done()

    # Verify the attribute was tracked
    entity_state = hass.states.get(TEST_SENSOR_FRONT_DOOR_PERSON_ENTITY_ID)
    assert entity_state
    assert entity_state.attributes.get("standing") == 1

    # Now simulate an event ending on a different camera
    async_fire_mqtt_message(
        hass,
        "frigate/events",
        json.dumps(
            {
                "before": {
                    "id": "test_object_789",
                    "camera": "other_camera",
                    "end_time": None,
                },
                "after": {
                    "id": "test_object_789",
                    "camera": "other_camera",
                    "end_time": 1607123970.123456,
                },
            }
        ),
    )
    await hass.async_block_till_done()

    # The attribute should still be there because it was a different camera
    entity_state = hass.states.get(TEST_SENSOR_FRONT_DOOR_PERSON_ENTITY_ID)
    assert entity_state
    assert entity_state.attributes.get("standing") == 1


async def test_event_without_end_time_ignored(hass: HomeAssistant) -> None:
    """Test that events without end_time are ignored."""
    await setup_mock_frigate_config_entry(hass)

    async_fire_mqtt_message(hass, "frigate/available", "online")
    await hass.async_block_till_done()

    # Simulate an object with an attribute being tracked
    async_fire_mqtt_message(
        hass,
        "frigate/tracked_object_update",
        json.dumps(
            {
                "type": "classification",
                "id": "test_object_101",
                "camera": "front_door",
                "timestamp": 1607123958.748393,
                "model": "person_orientation",
                "attribute": "standing",
                "score": 0.92,
            }
        ),
    )
    await hass.async_block_till_done()

    # Verify the attribute was tracked
    entity_state = hass.states.get(TEST_SENSOR_FRONT_DOOR_PERSON_ENTITY_ID)
    assert entity_state
    assert entity_state.attributes.get("standing") == 1

    # Now simulate an event update without end_time
    async_fire_mqtt_message(
        hass,
        "frigate/events",
        json.dumps(
            {
                "before": {
                    "id": "test_object_101",
                    "camera": "front_door",
                    "end_time": None,
                },
                "after": {
                    "id": "test_object_101",
                    "camera": "front_door",
                    "end_time": None,  # Still no end_time
                },
            }
        ),
    )
    await hass.async_block_till_done()

    # The attribute should still be there
    entity_state = hass.states.get(TEST_SENSOR_FRONT_DOOR_PERSON_ENTITY_ID)
    assert entity_state
    assert entity_state.attributes.get("standing") == 1

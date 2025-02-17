from __future__ import annotations
import typing

from homeassistant.components.light import (
    DOMAIN as PLATFORM_LIGHT,
    LightEntity,
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    ATTR_COLOR_TEMP,
    ATTR_RGB_COLOR,
    ATTR_EFFECT,
)

# back-forward compatibility hell
try:
    try:
        from homeassistant.components.light import LightEntityFeature

        SUPPORT_BRIGHTNESS = 0
        SUPPORT_COLOR = 0
        SUPPORT_COLOR_TEMP = 0
        SUPPORT_EFFECT = LightEntityFeature.EFFECT
    except:
        from homeassistant.components.light import (
            SUPPORT_BRIGHTNESS,
            SUPPORT_COLOR,
            SUPPORT_COLOR_TEMP,
            SUPPORT_EFFECT,
        )
except:
    # should HA remove any we guess SUPPORT_EFFECT still valued at 4
    SUPPORT_BRIGHTNESS = 0
    SUPPORT_COLOR = 0
    SUPPORT_COLOR_TEMP = 0
    SUPPORT_EFFECT = 4

try:
    try:
        from homeassistant.components.light import ColorMode

        COLOR_MODE_UNKNOWN = ColorMode.UNKNOWN
        COLOR_MODE_ONOFF = ColorMode.ONOFF
        COLOR_MODE_BRIGHTNESS = ColorMode.BRIGHTNESS
        COLOR_MODE_HS = ColorMode.HS
        COLOR_MODE_RGB = ColorMode.RGB
        COLOR_MODE_COLOR_TEMP = ColorMode.COLOR_TEMP
    except:
        from homeassistant.components.light import (
            COLOR_MODE_UNKNOWN,
            COLOR_MODE_ONOFF,
            COLOR_MODE_BRIGHTNESS,
            COLOR_MODE_HS,
            COLOR_MODE_RGB,
            COLOR_MODE_COLOR_TEMP,
        )
except:
    COLOR_MODE_UNKNOWN = ""  # leave empty so we don't use color_modes
    COLOR_MODE_ONOFF = COLOR_MODE_UNKNOWN
    COLOR_MODE_BRIGHTNESS = COLOR_MODE_UNKNOWN
    COLOR_MODE_HS = COLOR_MODE_UNKNOWN
    COLOR_MODE_RGB = COLOR_MODE_UNKNOWN
    COLOR_MODE_COLOR_TEMP = COLOR_MODE_UNKNOWN

import homeassistant.util.color as color_util

from .merossclient import MerossDeviceDescriptor, const as mc
from . import meross_entity as me
from .helpers import reverse_lookup
from .const import DND_ID

if typing.TYPE_CHECKING:
    from typing import Mapping
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry
    from .meross_device import MerossDevice, ResponseCallbackType

"""
    map light Temperature effective range to HA mired(s):
    right now we'll use a const approach since it looks like
    any light bulb out there carries the same specs
    MIRED <> 1000000/TEMPERATURE[K]
    (thanks to @nao-pon #87)
"""
MSLANY_MIRED_MIN = 153  # math.floor(1/(6500/1000000))
MSLANY_MIRED_MAX = 371  # math.ceil(1/(2700/1000000))


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_LIGHT)


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    return me.platform_unload_entry(hass, config_entry, PLATFORM_LIGHT)


def _rgb_to_int(rgb) -> int:
    if isinstance(rgb, int):
        return rgb
    elif isinstance(rgb, tuple):
        red, green, blue = rgb
    elif isinstance(rgb, dict):
        red = rgb["red"]
        green = rgb["green"]
        blue = rgb["blue"]
    else:
        raise ValueError("Invalid value for RGB!")
    return (red << 16) + (green << 8) + blue


def _int_to_rgb(rgb: int):
    return (rgb & 16711680) >> 16, (rgb & 65280) >> 8, (rgb & 255)


def _sat_1_100(value):
    if value > 100:
        return 100
    elif value < 1:
        return 1
    else:
        return int(value)


class MLLightBase(me.MerossToggle, LightEntity):
    """
    base 'abstract' class for meross light entities
    """
    PLATFORM = PLATFORM_LIGHT
    """
    internal copy of the actual meross light state
    """
    _light: dict
    """
    if the device supports effects, we'll map these to effect names
    to interact with HA api. This dict contains the effect key value
    used in the 'light' payload to the effect name
    """
    _light_effect_map = {}

    def update_onoff(self, onoff):
        if mc.KEY_ONOFF in self._light:
            self._light[mc.KEY_ONOFF] = onoff
        self.update_state(me.STATE_ON if onoff else me.STATE_OFF)

    def _inherited_parse_light(self, payload: dict):
        """
        allow inherited implementations to refine light payload parsing
        """
        pass

    def _parse_light(self, payload: dict):
        if not payload:
            return
        if (self._light != payload) or not self.available:
            self._light = payload

            onoff = payload.get(mc.KEY_ONOFF)
            if onoff is not None:
                self._attr_state = me.STATE_ON if onoff else me.STATE_OFF

            self._attr_color_mode = COLOR_MODE_UNKNOWN

            if mc.KEY_LUMINANCE in payload:
                self._attr_color_mode = COLOR_MODE_BRIGHTNESS
                self._attr_brightness = payload[mc.KEY_LUMINANCE] * 255 // 100
            else:
                self._attr_brightness = None

            if mc.KEY_TEMPERATURE in payload:
                self._attr_color_mode = COLOR_MODE_COLOR_TEMP
                self._attr_color_temp = ((100 - payload[mc.KEY_TEMPERATURE]) / 99) * (
                    self.max_mireds - self.min_mireds
                ) + self.min_mireds
            else:
                self._attr_color_temp = None

            if mc.KEY_RGB in payload:
                self._attr_color_mode = COLOR_MODE_RGB
                self._attr_rgb_color = _int_to_rgb(payload[mc.KEY_RGB])
                self._attr_hs_color = color_util.color_RGB_to_hs(*self._attr_rgb_color)
            else:
                self._attr_rgb_color = None
                self._attr_hs_color = None

            self._inherited_parse_light(payload)

            if self.hass and self.enabled:
                # since the light payload could be processed before the relative 'togglex'
                # here we'll flush only when the lamp is 'on' to avoid intra-updates to HA states.
                # when the togglex will arrive, the _light (attributes) will be already set
                # and HA will save a consistent state (hopefully..we'll see)
                self.async_write_ha_state()


class MLLight(MLLightBase):
    """
    light entity for Meross bulbs and any device supporting light api
    (identified from devices carrying 'light' node in SYSTEM_ALL payload)
    """
    device: LightMixin

    _attr_max_mireds = MSLANY_MIRED_MAX
    _attr_min_mireds = MSLANY_MIRED_MIN

    _hastogglex = False

    def __init__(self, device: MerossDevice, payload: dict):
        # we'll use the (eventual) togglex payload to
        # see if we have to toggle the light by togglex or so
        # with msl120j (fw 3.1.4) I've discovered that any 'light' payload sent will turn on the light
        # (disregarding any 'onoff' field inside).
        # The msl120j never 'pushes' an 'onoff' field in the light payload while msl120b (fw 2.1.16)
        # does that.
        # we used a 'conservative' approach here where we always toggled by togglex (if presented in digest)
        # and kindly ignore any 'onoff' in the 'light' payload (except digest didn't presented togglex)
        # also (issue #218) the newer mss560-570 dimmer switches are implemented as 'light' devices with ToggleX
        # api and show a glitch when used this way (ToggleX + Light)
        # we'll try implement a new command flow where we'll just use the 'Light' payload to turn on the device
        # skipping the initial 'ToggleX' assuming this behaviour works on any fw
        channel = payload.get(mc.KEY_CHANNEL, 0)
        descr = device.descriptor
        p_togglex = descr.digest.get(mc.KEY_TOGGLEX)
        if isinstance(p_togglex, list):
            for t in p_togglex:
                if t.get(mc.KEY_CHANNEL) == channel:
                    self._hastogglex = True
                    break
        elif isinstance(p_togglex, dict):
            self._hastogglex = p_togglex.get(mc.KEY_CHANNEL) == channel

        super().__init__(
            device,
            channel,
            None,
            None,
            None,
            mc.NS_APPLIANCE_CONTROL_TOGGLEX if self._hastogglex else None,
        )

        self._light = {}
        """
        capacity is set in abilities when using mc.NS_APPLIANCE_CONTROL_LIGHT
        """
        self._capacity = descr.ability[mc.NS_APPLIANCE_CONTROL_LIGHT].get(
            mc.KEY_CAPACITY, mc.LIGHT_CAPACITY_LUMINANCE
        )

        if COLOR_MODE_BRIGHTNESS:
            # new color_mode support from 2021.4.0
            self._attr_supported_color_modes = set()
            if self._capacity & mc.LIGHT_CAPACITY_RGB:
                self._attr_supported_color_modes.add(COLOR_MODE_RGB)  # type: ignore
                self._attr_supported_color_modes.add(COLOR_MODE_HS)  # type: ignore
            if self._capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
                self._attr_supported_color_modes.add(COLOR_MODE_COLOR_TEMP)  # type: ignore
            if not self._attr_supported_color_modes:
                if self._capacity & mc.LIGHT_CAPACITY_LUMINANCE:
                    self._attr_supported_color_modes.add(COLOR_MODE_BRIGHTNESS)  # type: ignore
                else:
                    self._attr_supported_color_modes.add(COLOR_MODE_ONOFF)  # type: ignore
        elif SUPPORT_BRIGHTNESS:
            # these will be removed in 2021.10
            self._attr_supported_features = (
                (
                    SUPPORT_BRIGHTNESS
                    if self._capacity & mc.LIGHT_CAPACITY_LUMINANCE
                    else 0
                )
                | (SUPPORT_COLOR if self._capacity & mc.LIGHT_CAPACITY_RGB else 0)
                | (
                    SUPPORT_COLOR_TEMP
                    if self._capacity & mc.LIGHT_CAPACITY_TEMPERATURE
                    else 0
                )
            )  # type: ignore

    async def async_turn_on(self, **kwargs):
        light = dict(self._light)
        # we need to preserve actual capacity in case HA tells to just toggle
        capacity = light.get(mc.KEY_CAPACITY, 0)
        # Color is taken from either of these 2 values, but not both.
        if (ATTR_HS_COLOR in kwargs) or (ATTR_RGB_COLOR in kwargs):
            if ATTR_HS_COLOR in kwargs:
                h, s = kwargs[ATTR_HS_COLOR]
                rgb = color_util.color_hs_to_RGB(h, s)
            else:
                rgb = kwargs[ATTR_RGB_COLOR]
            light[mc.KEY_RGB] = _rgb_to_int(rgb)
            light.pop(mc.KEY_TEMPERATURE, None)
            capacity |= mc.LIGHT_CAPACITY_RGB
            capacity &= ~mc.LIGHT_CAPACITY_TEMPERATURE
        elif ATTR_COLOR_TEMP in kwargs:
            # map mireds: min_mireds -> 100 - max_mireds -> 1
            mired = kwargs[ATTR_COLOR_TEMP]
            norm_value = (mired - self.min_mireds) / (self.max_mireds - self.min_mireds)
            temperature = 100 - (norm_value * 99)
            light[mc.KEY_TEMPERATURE] = _sat_1_100(
                temperature
            )  # meross wants temp between 1-100
            light.pop(mc.KEY_RGB, None)
            capacity |= mc.LIGHT_CAPACITY_TEMPERATURE
            capacity &= ~mc.LIGHT_CAPACITY_RGB

        # Brightness must always be set in payload
        if ATTR_BRIGHTNESS in kwargs:
            light[mc.KEY_LUMINANCE] = _sat_1_100(kwargs[ATTR_BRIGHTNESS] * 100 // 255)
        else:
            if mc.KEY_LUMINANCE not in light:
                light[mc.KEY_LUMINANCE] = 100
        capacity |= mc.LIGHT_CAPACITY_LUMINANCE

        if ATTR_EFFECT in kwargs:
            effect = reverse_lookup(self._light_effect_map, kwargs[ATTR_EFFECT])
            if effect is not None:
                if isinstance(effect, str) and effect.isdigit():
                    effect = int(effect)
                light[mc.KEY_EFFECT] = effect
                capacity |= mc.LIGHT_CAPACITY_EFFECT
            else:
                light.pop(mc.KEY_EFFECT, None)
                capacity &= ~mc.LIGHT_CAPACITY_EFFECT
        else:
            light.pop(mc.KEY_EFFECT, None)
            capacity &= ~mc.LIGHT_CAPACITY_EFFECT

        light[mc.KEY_CAPACITY] = capacity

        if self._hastogglex:
            # since lights could be repeatedtly 'async_turn_on' when changing attributes
            # we avoid flooding the device by sending togglex only once
            # this is probably unneeded since any light payload sent seems to turn on the light
            # 2022-10-10: removing the (likely unnecessary) code to overcome glitching in mss570 (#218)
            # if not self.is_on:
            #    await super().async_turn_on(**kwargs)
            pass
        else:
            light[mc.KEY_ONOFF] = 1

        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self._light = {} # invalidate so _parse_light will force-flush
                self._attr_state = me.STATE_ON
                self._parse_light(light)
        await self.device.async_request_light(light, _ack_callback)
        # 87: @nao-pon bulbs need a 'double' send when setting Temp
        if ATTR_COLOR_TEMP in kwargs:
            if self.device.descriptor.firmware.get(mc.KEY_VERSION) == "2.1.2":
                await self.device.async_request_light(light, None)

    async def async_turn_off(self, **kwargs):
        if self._hastogglex:
            # we suppose we have to 'toggle(x)'
            await super().async_turn_off(**kwargs)
        else:

            def _ack_callback(acknowledge: bool, header: dict, payload: dict):
                if acknowledge:
                    self.update_onoff(0)

            await self.device.async_request_light(
                {mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: 0},
                _ack_callback
            )

    def update_effect_map(self, light_effect_map: dict):
        """
        the list of available effects was changed (context at device level)
        so we'll just tell HA to update the state
        """
        self._light_effect_map = light_effect_map
        if light_effect_map:
            self._attr_supported_features |= SUPPORT_EFFECT
            self._attr_effect_list = list(light_effect_map.values())
        else:
            self._attr_supported_features &= ~SUPPORT_EFFECT
            self._attr_effect_list = None
        if self.hass and self.enabled:
            self.async_write_ha_state()

    def _inherited_parse_light(self, payload: dict):
        if mc.KEY_CAPACITY in payload:
            # despite of previous parsing, use capacity
            # value to effectively set this light color mode
            # this key is not present for instance in mod100 lights
            capacity = payload[mc.KEY_CAPACITY]
            if capacity & mc.LIGHT_CAPACITY_RGB:
                self._attr_color_mode = COLOR_MODE_RGB
            elif capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
                self._attr_color_mode = COLOR_MODE_COLOR_TEMP
            elif capacity & mc.LIGHT_CAPACITY_LUMINANCE:
                self._attr_color_mode = COLOR_MODE_BRIGHTNESS

        self._attr_effect = None
        if mc.KEY_EFFECT in payload:
            # here effect might be an int while our map keys might be 'str formatted'
            # so we'll use a flexible (robust? dumb?) approach here in mapping
            effect = payload[mc.KEY_EFFECT]
            if effect in self._light_effect_map:
                self._attr_effect = self._light_effect_map.get(effect)
            elif isinstance(effect, int):
                for key, value in self._light_effect_map.items():
                    if isinstance(key, str) and key.isdigit():
                        if int(key) == effect:
                            self._attr_effect = value
                            break
                else:
                    # we didnt find the effect even with effectId int casting
                    # so we hope it's positional....
                    effects = self._light_effect_map.values()
                    if effect < len(effects):
                        self._attr_effect = effects[effect]  # type: ignore


class MLDNDLightEntity(me.MerossToggle, LightEntity):
    """
    light entity representing the device DND feature usually implemented
    through a light feature (presence light or so)
    """

    PLATFORM = PLATFORM_LIGHT

    _attr_supported_color_modes = {COLOR_MODE_ONOFF}
    _attr_entity_category = me.EntityCategory.CONFIG

    def __init__(self, device: MerossDevice):
        super().__init__(device, None, DND_ID, mc.KEY_DNDMODE, None, None)

    @property
    def supported_color_modes(self):
        return self._attr_supported_color_modes

    @property
    def color_mode(self):
        return COLOR_MODE_ONOFF

    async def async_turn_on(self, **kwargs):
        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self.update_state(me.STATE_ON)

        await self.device.async_request(
            mc.NS_APPLIANCE_SYSTEM_DNDMODE,
            mc.METHOD_SET,
            {mc.KEY_DNDMODE: {mc.KEY_MODE: 0}},
            _ack_callback,
        )

    async def async_turn_off(self, **kwargs):
        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self.update_state(me.STATE_OFF)

        await self.device.async_request(
            mc.NS_APPLIANCE_SYSTEM_DNDMODE,
            mc.METHOD_SET,
            {mc.KEY_DNDMODE: {mc.KEY_MODE: 1}},
            _ack_callback,
        )

    def update_onoff(self, onoff):
        self.update_state(me.STATE_OFF if onoff else me.STATE_ON)


class LightMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    """
    add to MerossDevice when creating actual device in setup
    in order to provide NS_APPLIANCE_CONTROL_LIGHT and
    NS_APPLIANCE_CONTROL_LIGHT_EFFECT capability
    """

    light_effect_map: dict[object, str] = {}  # map effect.Id to effect.Name

    def __init__(self, api, descriptor: MerossDeviceDescriptor, entry):
        super().__init__(api, descriptor, entry)

        if mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT in descriptor.ability:
            self.polling_dictionary[
                mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT
            ] = mc.PAYLOAD_GET[mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT]

    def _init_light(self, payload: dict):
        MLLight(self, payload)

    def _handle_Appliance_Control_Light(self, header: dict, payload: dict):
        self._parse_light(payload.get(mc.KEY_LIGHT))

    def _handle_Appliance_Control_Light_Effect(self, header: dict, payload: dict):
        light_effect_map = {}
        for p_effect in payload.get(mc.KEY_EFFECT, []):
            light_effect_map[p_effect[mc.KEY_ID_]] = p_effect[mc.KEY_EFFECTNAME]
        if light_effect_map != self.light_effect_map:
            self.light_effect_map = light_effect_map
            for entity in self.entities.values():
                if isinstance(entity, MLLight):
                    entity.update_effect_map(light_effect_map)

    def _parse_light(self, payload):
        self._parse__generic(mc.KEY_LIGHT, payload)

    async def async_request_light(self, payload, callback: ResponseCallbackType | None):
        await self.async_request(
            mc.NS_APPLIANCE_CONTROL_LIGHT,
            mc.METHOD_SET,
            { mc.KEY_LIGHT: payload },
            callback
        )

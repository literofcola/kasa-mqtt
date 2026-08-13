#!/usr/bin/env python
import asyncio
import collections
import random
import json
import colorsys
from typing import Dict
from dataclasses import dataclass
from contextlib import AsyncExitStack, asynccontextmanager
from random import randrange
from aiomqtt import Client, MqttError

from kasa_mqtt import log
from kasa_mqtt.config import Cfg

from kasa import Discover, Device, Module, KasaException, Credentials
from kasa.exceptions import AuthenticationError

@dataclass
class KasaDevice:
    topic: str
    name: str
    host: str
    username: str
    password: str
    _device: Device

    def __init__(self, topic: str, name: str, host: str, username: str = None, password: str = None):
        self.name = name
        self.topic = topic
        self.host = host
        # Only needed for devices requiring TP-Link cloud account credentials
        # (e.g. newer Tapo devices using the TPAP encryption scheme). Devices
        # that don't need them can leave these as None - existing behaviour
        # for KLAP/legacy Kasa devices is unaffected.
        self.username = username
        self.password = password
        self._device = None
        assert self.host

    async def _get_device(self):
        if not self._device:
            if self.host:
                try:
                    if self.username and self.password:
                        creds = Credentials(self.username, self.password)
                        self._device = await Discover.discover_single(self.host, credentials=creds)
                    else:
                        self._device = await Discover.discover_single(self.host)
                    # update() must be called at least once so the device's
                    # modules (e.g. Module.Light, used for HSV/brightness) get
                    # populated - discover_single() alone does not do this.
                    await self._device.update()
                    logger.debug(
                        f"Discovered {self.host}"
                        f" model:{self._device.model}"
                        f" mac:{self._device.mac}"
                    ) 
                except AuthenticationError as e:
                    logger.error(f"{self.host} authentication failed - check username/password in config: {e}")
                    self._device = None
                except KasaException as e:
                    logger.debug(f"discover_single error: {e}")
                    self._device = None
                except Exception as e:
                    # update() can raise non-KasaException errors too - e.g. a
                    # zoneinfo.ZoneInfoNotFoundError if the device reports a
                    # legacy timezone name (like "PST8PDT") that isn't in the
                    # system's tzdata. Don't let that crash the whole service;
                    # `pip install tzdata` usually fixes the root cause.
                    logger.error(f"{self.host} update() failed: {e}")
                    self._device = None
        return self._device

    async def turn_on(self):
        try:
            device = await self._get_device()
            await device.turn_on()
        except AttributeError as e:
            logger.error(f"{self.host} _get_device failed: {e}")       
        except KasaException as e:
            logger.error(f"{self.host} unable to turn_on: {e}")

    async def turn_off(self):
        try:
            device = await self._get_device()
            await device.turn_off()
        except AttributeError as e:
            logger.error(f"{self.host} _get_device failed: {e}")  
        except KasaException as e:
            logger.error(f"{self.host} unable to turn_off: {e}")

    async def SetColor_HSV(self, wanted_hsv: tuple):
        try:
            device = await self._get_device()
            light = device.modules.get(Module.Light)
            if light is None:
                logger.error(f"{self.host} does not support the Light module (no color control)")
                return
            await light.set_hsv(int(wanted_hsv[0]*360), int(wanted_hsv[1]*100), int(wanted_hsv[2]*100))
        except AttributeError as e:
            logger.error(f"{self.host} _get_device failed: {e}")  
        except KasaException as e:
            logger.error(f"{self.host} unable to set_hsv: {e}")
        except ValueError as e:
            logger.error(f"{self.host} unable to set_hsv: {e}")

    async def SetBrightness(self, wanted_brightness: int):
        if wanted_brightness < 0 or wanted_brightness > 100:
            return
        try:
            device = await self._get_device()
            light = device.modules.get(Module.Light)
            if light is None:
                logger.error(f"{self.host} does not support the Light module (no brightness control)")
                return
            await light.set_brightness(wanted_brightness)
        except AttributeError as e:
            logger.error(f"{self.host} _get_device failed: {e}")  
        except KasaException as e:
            logger.error(f"{self.host} unable to set_brightness: {e}")
        except ValueError as e:
            logger.error(f"{self.host} unable to set_brightness: {e}")


# indexed by unique key KasaDevice.topic
device_list: Dict[str, KasaDevice] = {}
running = True

async def main_loop():
    global device_list
    global running

    tasks = set()
    
    logger.debug("Starting main event processing loop")
    cfg = Cfg()
    mqtt_broker_ip = cfg.mqtt_host
    mqtt_client_id = cfg.mqtt_client_id

    async with AsyncExitStack() as stack:
        # Keep track of the asyncio tasks that we create, so that we can cancel them on exit
        stack.push_async_callback(cancel_tasks, tasks)

        # Connect to the MQTT broker
        client = Client(hostname=mqtt_broker_ip, identifier=mqtt_client_id)
        await stack.enter_async_context(client)

        # aiomqtt no longer has filtered_messages()/unfiltered_messages() context
        # managers - all incoming messages now come through client.messages, and
        # we filter by topic ourselves in MQTT_Receive_Callback.
        task = asyncio.create_task(MQTT_Receive_Callback(client))
        tasks.add(task)

        # Create the device list and subscribe to their topics
        for device_name, config in cfg.devices.items():
            device_topic = cfg.mqtt_topic(device_name)
            device_host = cfg.devices.get(device_name, {}).get('host')
            # Optional TP-Link cloud credentials, only required for devices
            # using the newer TPAP encryption scheme (e.g. some Tapo bulbs).
            # Leave unset in config.yaml for existing KLAP/legacy devices.
            device_username = cfg.devices.get(device_name, {}).get('username')
            device_password = cfg.devices.get(device_name, {}).get('password')
            device_list[device_topic] = KasaDevice(device_topic, device_name, device_host, device_username, device_password)
            await device_list[device_topic]._get_device()
            logger.info(f"Adding {device_list[device_topic]} to device list")
            await client.subscribe(device_topic)
            logger.info(f"Subscribing to topic {device_topic}")

        # Subscribe to topic to control kasa_mqtt
        await client.subscribe("kasa_mqtt_control")

        # task = asyncio.create_task(MQTT_Post(client))
        # tasks.add(task)

        # Wait for everything to complete (or fail due to, e.g., network errors)
        await asyncio.gather(*tasks)  

async def MQTT_Receive_Callback(client):
    global device_list
    global running

    async for message in client.messages:
        topic = message.topic.value
        logger.debug(f"{topic} | {message.payload.decode()}")

        # Check if the received message topic matches one of our devices
        if device_list.get(topic, None):

            try:
                json_state = json.loads(message.payload.decode())
                is_json = True
            except ValueError as e:
                # logger.debug(f"handle_kasa_requests received non-json payload")
                is_json = False
            except TypeError as e:
                # logger.debug(f"handle_kasa_requests received non-json payload")
                is_json = False

            if is_json == False and message.payload.decode() == 'on':
                await device_list[topic].turn_on()

            if is_json == False and message.payload.decode() == 'off':
                await device_list[topic].turn_off()

            # Change values based on json
            if is_json == True:
                if 'state' in json_state and json_state['state'] == "on":
                    await device_list[topic].turn_on()
                if 'state' in json_state and json_state['state'] == "off":
                    await device_list[topic].turn_off()
                if 'brightness' in json_state:
                    await device_list[topic].SetBrightness(int(json_state['brightness']))
                    
            #parse as Hex RGB
            if is_json == False and "#" in message.payload.decode(): 
                wanted_hex = message.payload.decode().lstrip('#')
                wanted_rgb = tuple(int(wanted_hex[i:i+2], 16) for i in (0, 2, 4))
                wanted_rgb = tuple(x/255 for x in wanted_rgb)
                wanted_hsv = colorsys.rgb_to_hsv(*wanted_rgb)
                #logger.debug(f"{device_list[topic].name} @ {device_list[topic].host} SetColor_HSV {wanted_rgb}")
                await device_list[topic].SetColor_HSV(wanted_hsv)

        if topic == "kasa_mqtt_control":
            if message.payload.decode() == 'shutdown':
                running = False
                break
        if topic == "kasa_mqtt_control":
            if message.payload.decode() == 'restart':
                break

# async def MQTT_Post(client):
#     while True:
#         message = randrange(100)
#         print(f'[topic="/kasa_mqtt_test_topic/"] Publishing message={message}')
#         await client.publish("/kasa_mqtt_test_topic/", message, qos=1)
#         await asyncio.sleep(2)

async def cancel_tasks(tasks):
    logger.debug(f"cancel_tasks tasks={tasks}")
    for task in tasks:
        if task.done():
            continue
        try:
            task.cancel()
            await task
        except asyncio.CancelledError:
            pass
            

async def main():
    global running
    # Run the main_loop indefinitely. Reconnect automatically if the connection is lost.
    reconnect_interval = Cfg().reconnect_interval
    while running:
        try:
            await main_loop()
        except MqttError as error:
            logger.debug(f'Error "{error}". Reconnecting in {reconnect_interval} seconds.')
            logger.debug(f"finally: await asyncio.sleep(reconnect_interval) running={running}")
            await asyncio.sleep(reconnect_interval)
        except (KeyboardInterrupt, SystemExit):
            logger.debug("got KeyboardInterrupt")
            running = False
            break
        except asyncio.CancelledError:
            logger.debug(f"main(): got asyncio.CancelledError running={running}")
            running = False
            break
        except Exception as error:
            logger.debug(f'Error "{error}".')
            running = False
            break


if __name__ == "__main__":
    logger = log.getLogger()
    log.initLogger()

    knobs = Cfg().knobs
    if isinstance(knobs, collections.abc.Mapping):
        if knobs.get("log_to_console"):
            log.log_to_console()
        if knobs.get("log_level_debug"):
            log.set_log_level_debug()

    logger.info("kasa_mqtt process started")
    asyncio.run(main())
    logger.debug("kasa_mqtt process stopped")

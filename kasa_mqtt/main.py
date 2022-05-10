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
from asyncio_mqtt import Client, MqttError

from kasa_mqtt import log
from kasa_mqtt.config import Cfg

from kasa import Discover
from kasa.smartdevice import SmartDevice, SmartDeviceException

@dataclass
class KasaDevice:
    topic: str
    name: str
    host: str
    _device: SmartDevice

    def __init__(self, topic: str, name: str, host: str):
        self.name = name
        self.topic = topic
        self.host = host
        self._device = None
        assert self.host

    async def _get_device(self) -> SmartDevice:
        if not self._device:
            if self.host:
                try:
                    self._device = await Discover.discover_single(self.host)
                    logger.debug(
                        f"Discovered {self.host}"
                        f" model:{self._device.model}"
                        f" mac:{self._device.mac}"
                    ) 
                except SmartDeviceException as e:
                    logger.debug(f"discover_single error: {e}")
        return self._device

    async def turn_on(self):
        try:
            device = await self._get_device()
            await device.turn_on()
        except AttributeError as e:
            logger.error(f"{self.host} _get_device failed: {e}")       
        except SmartDeviceException as e:
            logger.error(f"{self.host} unable to turn_on: {e}")

    async def turn_off(self):
        try:
            device = await self._get_device()
            await device.turn_off()
        except AttributeError as e:
            logger.error(f"{self.host} _get_device failed: {e}")  
        except SmartDeviceException as e:
            logger.error(f"{self.host} unable to turn_off: {e}")

    async def SetColor_HSV(self, wanted_hsv: tuple):
        try:
            device = await self._get_device()
            await device.set_hsv(int(wanted_hsv[0]*360), int(wanted_hsv[1]*100), int(wanted_hsv[2]*100))
        except AttributeError as e:
            logger.error(f"{self.host} _get_device failed: {e}")  
        except SmartDeviceException as e:
            logger.error(f"{self.host} unable to set_hsv: {e}")
        except ValueError as e:
            logger.error(f"{self.host} unable to set_hsv: {e}")

    async def SetBrightness(self, wanted_brightness: int):
        if wanted_brightness < 0 or wanted_brightness > 100:
            return
        try:
            device = await self._get_device()
            await device.set_brightness(wanted_brightness)
        except AttributeError as e:
            logger.error(f"{self.host} _get_device failed: {e}")  
        except SmartDeviceException as e:
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
        client = Client(hostname=mqtt_broker_ip, client_id=mqtt_client_id, keepalive=0)
        await stack.enter_async_context(client)

        # Messages that doesn't match a filter will get sent to MQTT_Receive_Callback
        messages = await stack.enter_async_context(client.unfiltered_messages())
        task = asyncio.create_task(MQTT_Receive_Callback(messages))
        tasks.add(task)

        # Create the device list and subscribe to their topics
        for device_name, config in cfg.devices.items():
            device_topic = cfg.mqtt_topic(device_name)
            device_host = cfg.devices.get(device_name, {}).get('host')
            device_list[device_topic] = KasaDevice(device_topic, device_name, device_host)
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

async def MQTT_Receive_Callback(messages):
    global device_list
    global running

    async for message in messages:
        logger.debug(f"{message.topic} | {message.payload.decode()}")

        # Check if the received message topic matches one of our devices
        if device_list.get(message.topic, None):

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
                await device_list[message.topic].turn_on()

            if is_json == False and message.payload.decode() == 'off':
                await device_list[message.topic].turn_off()

            # Change values based on json
            if is_json == True:
                if 'state' in json_state and json_state['state'] == "on":
                    await device_list[message.topic].turn_on()
                if 'state' in json_state and json_state['state'] == "off":
                    await device_list[message.topic].turn_off()
                if 'brightness' in json_state:
                    await device_list[message.topic].SetBrightness(int(json_state['brightness']))
                    
            #parse as Hex RGB
            if is_json == False and "#" in message.payload.decode(): 
                wanted_hex = message.payload.decode().lstrip('#')
                wanted_rgb = tuple(int(wanted_hex[i:i+2], 16) for i in (0, 2, 4))
                wanted_rgb = tuple(x/255 for x in wanted_rgb)
                wanted_hsv = colorsys.rgb_to_hsv(*wanted_rgb)
                #logger.debug(f"{device_list[message.topic].name} @ {device_list[message.topic].host} SetColor_HSV {wanted_rgb}")
                await device_list[message.topic].SetColor_HSV(wanted_hsv)

        if message.topic == "kasa_mqtt_control":
            if message.payload.decode() == 'shutdown':
                running = False
                break
        if message.topic == "kasa_mqtt_control":
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

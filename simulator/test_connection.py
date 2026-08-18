import paho.mqtt.client as mqtt
import time

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.connect("localhost", 1883, 60)
client.loop_start()

result = client.publish("coldchain/test", "hello from python")
result.wait_for_publish()

time.sleep(1)
client.loop_stop()
client.disconnect()

print("Message sent successfully")

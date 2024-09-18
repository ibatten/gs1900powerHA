#!/usr/bin/python3

"""read data from remote Zyxel switch and publish it for power monitoring"""

# pylint: disable=invalid-name

import re
import json
from time import sleep
import sys
import signal
import pexpect
import paho.mqtt.publish

MQTTUSER = "remotesensors"
MQTTPASS = "remotesensors"
MQTTAUTH = {"username": MQTTUSER, "password": MQTTPASS}
SSHUSER = "igb"
SSHPASS = "***REMOVED***"
CLIPROMPT = "# "

newlines = re.compile(r"[\r\n]+")
hostchars = re.compile(r"[-A-Za-z0-9.]+$")
# 1    On    Normal  77Watts  16Watts ( 21%)  16Watts  60Watts
# Port Power Limit (Admin) (mW) Power (mW) Voltage (mV) Current (mA)

# 4    31200 (31200)            5900       53429        111

#  show power inline consumption
#  Power management mode: Port limit mode
#  Pre-allocation       : Disabled
#  Power-up sequence    : Staggered
#  Unit Power Status Nominal  Allocated       Consumed Available
#                    Power    Power           Power    Power
#  ---- ----- ------ -------- --------------- -------- ---------
#  1    On    Normal  77Watts  16Watts ( 21%)  16Watts  60Watts
#  Port Power Limit (Admin) (mW) Power (mW) Voltage (mV) Current (mA)
#  ---- ------------------------ ---------- ------------ ------------

portspec = re.compile(
    r"""
    ^(?P<port>[1-8])   \s+
     (?P<onpower>\d+)  \s+
   \((?P<limit>\d+     \s*)\) \s+
     (?P<power>\d+)    \s+
     (?P<voltage>\d+)  \s+
     (?P<current>\d+)""",
    re.VERBOSE,
)

skip = re.compile(
    r"""^(?:
   show.*ion |
   Power.*mode |
   Pre-allo.*abled |
   Power-up.*ed |
   Unit.*able |
   \s+.*Power |
   [- ]+ |
   \d.*Watts |
   Port.*\(mA\)   )\s*$""",
    re.VERBOSE,
)


channels = {}


def parse(s):
    """split on one or more newlines or equivalent"""
    return newlines.split(s.decode("utf-8"))

def signal_handler(sig, frame):
    for (channel, _) in channels.values ():
        try:
            channel.close () 
        except:
            pass
    sys.exit (0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGHUP, signal_handler)



def connect_to(host):
    """connect to host and do initial exchanges to establish the full prompt"""
    child = pexpect.spawn(
        f"ssh -o StrictHostKeyChecking=accept-new {host} -l {SSHUSER}"
    )
    child.expect("password: ")
    child.sendline(SSHPASS)
    child.expect(CLIPROMPT)
    p = parse(child.before)
    m = hostchars.search(p[-1])
    prompt = (m.group(0))[1:] + CLIPROMPT
    return (child, prompt)


channels = {}


def update_host(host, reportinghost):
    """on host, run the show power inline consumptions and parse the result.  return
       the messages we are going to need to send"""
    try:
        info = channels[host]
    except KeyError:
        try:
            info = connect_to(host)
            channels[host] = info
        except (pexpect.TIMEOUT, pexpect.EOF) as e:
            raise RuntimeError(f"cannot connect to {host}") from e

    try:
        (child, prompt) = info
        child.sendline()
        child.expect(prompt)
        child.sendline("show power inline consumption")
        child.expect(prompt)
        p = parse(child.before)
    except (pexpect.TIMEOUT, pexpect.EOF, TypeError):
        del channels[host]
        raise RuntimeError(f"comms failure to {host}") from e

    total = 0
    linepower = {}
    for line in p:
        if line:
            m = portspec.match(line)
            if m:
                power = (
                    int(m.group("voltage")) / 1000.0 * int(m.group("current")) / 1000.0
                )
                total += power
                linepower["port" + m.group("port")] = f"{power:.2f}"
            elif skip.match(line):
                pass
            else:
                print(f"no match '{line}'")

    return [
        {"topic": f"poe/{reportinghost}/total", "payload": f"{total:.2f}"},
        # {"topic": f"poe/{reportinghost}/attributes", "payload": json.dumps(linepower)},
    ]


def discovery_records(tidyhostlist):
    """make discovery records for a list of (legal) names"""
    def one_record(host):
        """make one discovery record"""
        j = {
            "name": f"{host} POE Consumption",
            "state_topic": f"poe/{host}/total",
            "device_class": "power",
            "unit_of_measurement": "W",
            "value_template": "{{ value }}",
            "unique_id": f"{host}_poe_total",
            "device": {
                "name": f"Ethernet Switch {host}",
                "manufacturer": "Zyxel",
                "model": "GS1900-10HP",
                "identifiers": [host],
            },
            # "json_attributes_topic": f"poe/${host}/attributes",
            # "json_attributes_template": "{{ value_json.data.value | tojson }}",
        }
        return json.dumps(j)

    # https://community.home-assistant.io/t/mqtt-sensor-add-attributes-while-remaining-in-autodiscovery/578273/5

    discovery = [
        {
            "topic": f"homeassistant/sensor/{host}_poe/config",
            "payload": one_record(host),
        }
        for host in tidyhostlist
    ]

    return discovery


def main(live=False):
    """do the heavy lifting"""
    hosts = [f"gs1900-10hp-{h}" for h in (1, 2, 3)]
    tidyhosts = [t.replace("-", "_") for t in hosts]

    discovery = discovery_records(tidyhosts)
    if live:
        paho.mqtt.publish.multiple(
            discovery, hostname="homeassistant***REMOVED***", auth=MQTTAUTH
        )
    else:
        print(discovery)

    while True:
        results = []
        for host, reportinghost in zip(hosts, tidyhosts):
            try:
                results += update_host(host + ".home***REMOVED***", reportinghost)
            except RuntimeError as e:
                print(e)
        if results and live:
            paho.mqtt.publish.multiple(
                results, hostname="homeassistant***REMOVED***", auth=MQTTAUTH
            )
        else:
            print(results)
            print(f"published {len(results)} records")
        sleep(30)

if len(sys.argv)>1 and sys.argv[1] == 'live':
    main(True)
else:
    main(False)

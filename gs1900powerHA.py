#!/usr/bin/python3

"""read data from remote Zyxel switch and publish it for power monitoring"""

# pylint: disable=invalid-name

import re
import json
from time import sleep
import sys
import signal
import configparser
import argparse

import pexpect
import paho.mqtt.publish

DEBUG = False


def debug_print(s):
    """print iff DEBUG is set"""
    if DEBUG:
        print(s)


def getargs():
    """args parsed"""

    # pylint: disable=global-statement
    global DEBUG

    parser = argparse.ArgumentParser(description="Collect power data from switches")
    parser.add_argument("--live", help="run for real", action="store_true")
    parser.add_argument(
        "--cf", action="store", default="/etc/gs1900powerHA.cf", help="config file"
    )
    parser.add_argument("--debug", help="ad hoc tracing", action="store_true")
    p = parser.parse_args()
    DEBUG = p.debug
    return p


def readcf(file):
    """read in the config"""

    debug_print(f"reading {file}")
    cf = configparser.ConfigParser()
    cf.read(file)
    return cf


# pylint: disable=too-many-instance-attributes
class GS1900:
    """Model a GS1900-10HP (and maybe others) switch"""

    instances = []

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

    @staticmethod
    def parse(s):
        """split on one or more newlines or equivalent"""
        return GS1900.newlines.split(s.decode("utf-8"))

    def shutdown(self):
        """close a channel if it's open"""
        if self.child:
            self.child.close()
        self.child = None

    # pylint: disable=unused-argument
    @staticmethod
    def signal_handler(sig, frame):
        """handle SIGINT and SIGHUP"""
        for i in GS1900.instances:
            i.shutdown()
        sys.exit(0)


    def __init__(self, host, cf):
        debug_print(f"connecting to '{host}'")

        def tidyhost(host):
            """the form we need for reporting and mqtt"""
            return host.split(".", 1)[0].replace("-", "_")

        self.host = host
        self.reportinghost = tidyhost(host)
        ssh = cf[host]["ssh"]
        ssh_username = cf[host]["username"]
        self.ssh_password = cf[host]["password"]
        self.ssh_cmd = f"{ssh} -l {ssh_username} {host}"
        self.bare_prompt = self.CLIPROMPT
        self.prompt = self.CLIPROMPT
        self.command = cf[host]["command"]
        self.child = None

    def connect(self):
        """connect to host and do initial
        exchanges to establish the full prompt"""

        try:
            debug_print(self.ssh_cmd)
            self.child = pexpect.spawn(self.ssh_cmd)
            self.child.expect("password: ")
            self.child.sendline(self.ssh_password)
            self.child.expect(self.bare_prompt)
        except (pexpect.TIMEOUT, pexpect.EOF) as e:
            debug_print(f"cannot connect: {self.ssh_cmd} {e}")
            self.shutdown()
            return

        p = self.parse(self.child.before)
        m = self.hostchars.search(p[-1])
        self.prompt = (m.group(0))[1:] + self.bare_prompt

    def get_output(self):
        """on host, run command and return result as a list of lines"""

        debug_print(f"get_output {self.host}")
        if not self.child:
            self.connect()

        if not self.child:
            debug_print(f"get_output has null child {self.host}")
            return None

        try:
            self.child.sendline()
            self.child.expect(self.prompt)
            self.child.sendline(self.command)
            self.child.expect(self.prompt)
            p = self.parse(self.child.before)
        except (pexpect.TIMEOUT, pexpect.EOF):
            debug_print(f"shutting down {self.ssh_cmd}")
            self.shutdown()

        debug_print(f"returning {p}")
        return p

    def get_update(self):
        """on host, run the show power inline consumptions and parse
        the result.  return the messages we are going to need to send
        """

        p = self.get_output()
        if not p:
            return None

        total = 0
        linepower = {}
        for line in p:
            if line:
                m = self.portspec.match(line)
            if m:
                power = (
                    int(m.group("voltage")) / 1000.0 * int(m.group("current")) / 1000.0
                )
                total += power
                linepower["port" + m.group("port")] = f"{power:.2f}"
            elif self.skip.match(line):
                pass
            else:
                debug_print(f"no match '{line}'")

        return [
            {"topic": f"poe/{self.reportinghost}/total", "payload": f"{total:.2f}"},
            {
                "topic": f"poe/{self.reportinghost}/attributes",
                "payload": json.dumps(linepower),
            },
        ]

    def discovery_record(self):
        """make discovery record for a list of (legal) names"""

        name = self.reportinghost
        j = {
            "name": f"{name} POE Consumption",
            "state_topic": f"poe/{name}/total",
            "device_class": "power",
            "unit_of_measurement": "W",
            "value_template": "{{ value }}",
            "unique_id": f"{name}_poe_total",
            "device": {
                "name": f"Ethernet Switch {self.host}",
                "manufacturer": "Zyxel",
                "model": "GS1900-10HP",
                "identifiers": [self.host],
            },
            "json_attributes_topic": f"poe/{name}/attributes",
        }
        return {
            "topic": f"homeassistant/sensor/{name}_poe/config",
            "payload": json.dumps(j),
        }


def main():
    """do the heavy lifting"""

    args = getargs()
    cf = readcf(args.cf)
    mqttauth = {"username": cf["mqtt"]["username"], "password": cf["mqtt"]["password"]}
    debug_print (mqttauth)

    signal.signal(signal.SIGINT, GS1900.signal_handler)
    signal.signal(signal.SIGHUP, GS1900.signal_handler)

    # danger here: if you don't force it to a tuple, then it's a generator
    # and you can only read it once

    hosts = tuple(
        GS1900(x, cf)
        for x in cf.sections()
        if x and x not in ("DEFAULT", "mqtt", "global")
    )

    if not hosts:
        return

    results = []

    # using a list comprehension results in a generator,
    # which is wrong because we want to loop over this
    # repeatedly.  list([comprehension]) is hardly better.

    for host in hosts:
        results.append(host.discovery_record())

    try:
        loopdelay = int(cf["global"]["sleep"])
    except KeyError:
        loopdelay = 30

    while True:
        for host in hosts:
            debug_print(f"updating {host.host}")
            r = host.get_update()
            if r:
                results += r

        if results:
            if args.live:
                debug_print (results)
                paho.mqtt.publish.multiple(
                    results, hostname=cf["mqtt"]["host"], auth=mqttauth
                )
            else:
                print(results)
            results = []

        sleep(loopdelay)


main()

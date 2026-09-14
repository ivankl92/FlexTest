# SYSTEM: system description of the TSN-FlexTest testbed

The TSN-FlexTest is designed for evaluation and benchmarking of the TSN.

---

## 1. The testbed

The testbed consists of following devices:

### Endpoints

2x UP Squared Pro 7000 Edge PCs with Intel I226-IT Ethernet Controller
4x Rspberry PI 5B
5x NXP MIMXRT1170-EVKB (CTRL, IO-D0, ENC0, IO-D1, ENC1)

### TSN Switches
5x Kontron KSwitch D10 MMT TSN Switches

### Addresses

The davices have following addresses:

		IPv4			MAC	
UP-1	192.168.1.61	00:07:32:C1:43:30
		192.168.1.62	00:07:32:C1:43:31
						
UP-2	192.168.1.71	00:07:32:C1:29:69
		192.168.1.72	00:07:32:C1:29:6A
		
RPI1	192.168.1.51	88:a2:9e:4b:97:1b
RPI2	192.168.1.52	88:a2:9e:a6:d1:5f
RPI3	192.168.1.53	88:a2:9e:a6:cb:86
RPI4	192.168.1.54	88:a2:9e:a6:bb:a4
SW1		192.168.1.10	00:80:82:b9:65:33
SW2		192.168.1.11	00:80:82:bd:25:7c
SW3		192.168.1.12	00:80:82:bd:25:a2
SW4		192.168.1.13	00:80:82:bd:25:80
SW5		192.168.1.14	00:80:82:bd:25:72
CTRL	192.168.1.120	00-BB-CC-DD-EE-12
IO-D0	192.168.1.130	00-BB-CC-DD-EE-13
ENC0	192.168.1.140	00-BB-CC-DD-EE-14
IO-D1	192.168.1.150	00-BB-CC-DD-EE-15
ENC1	192.168.1.160	00-BB-CC-DD-EE-16

### NETCONF and YANG

Each switch supports NETCONF protocol and YANG models. The netconf host address corresponds to the ip address of the switch. Login: netconf, password: geheim.

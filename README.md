ActionBan
===========

ActionBan is a Python daemon that maintains counters of specific actions
performed by a specific ip over the last 60 seconds and "jails" an ip address
(adds it to an ipset) if it exceeds specified thresholds. Thresholds are checked
every second.

Aims of the software were to allow a more direct and high volume vector for 
banning abusive users directly from an application, instead of being forced
to use log files as fail2ban does. Fail2ban frequently chokes with hundreds of 
failures per second, making it unsuitable for certain types of banning actions.

**Main Design Points**

* Benchmarked at over 20,000 actions per second on 160,000 jails, ActionBan's simplicity allows it to handle (decently) high volume.
* Mostly configurationless, jail configuration are actually sent with each action logging request allowing easy dynamic creation of jails.
* Persistent. (I believe) Fail2Ban stores no state, meaning a restart usually unbans currently banned ips (although it backtracks through logs). All current bans are persisted to file.
* Uses UDP to un-encumber the sending application. Under high loads you don't want to waste cycles waiting for ACKs.

**Still largely in development, not recommended for production use.**

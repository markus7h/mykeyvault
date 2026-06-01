# requirements for mykeyvault

1. nothing beside a claude cli helper oder mcp need to be installed on a new claude system
2. idea is to have scrips or environment variables handling keys and secrets to prevent secret poluting into claude context
3. i want mykeyvault and bw to connect to mykeyvault running in a container on a docker host
4. credentials to connect to mykeyvault are local to the container running bw (client)
5. open question is regarding to point 1. how is connection and workflow handelt from claude to bw client container without using ssh or similar, just talking to an api that offers:
- save secrets of multiple kind, like keys, usernames, token, ...
- get secrets to establish a connection to api, ssh, cloud service, smb-share, ...
- remove secrets
- update or modify secrets
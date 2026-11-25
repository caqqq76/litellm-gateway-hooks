from fakeredis import TcpFakeServer

server = TcpFakeServer(("127.0.0.1", 6390), server_type="redis")
server.serve_forever()

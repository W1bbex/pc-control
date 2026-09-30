import sys
import re

def check_timeouts(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    stop_pos = content.find('secureClient.stop();')
    timeout_pos = content.find('secureClient.setTimeout(3000);')
    connect_pos = content.find('mqtt.connect(')
    
    if stop_pos == -1:
        print("ERROR: secureClient.stop() not found in connectMqtt()")
        sys.exit(1)
    
    if timeout_pos == -1:
        print("ERROR: secureClient.setTimeout(3000); not found in connectMqtt()")
        sys.exit(1)
        
    if connect_pos == -1:
        print("ERROR: mqtt.connect( not found in connectMqtt()")
        sys.exit(1)
        
    if not (stop_pos < timeout_pos < connect_pos):
        print(f"ERROR: Invalid call order. stop={stop_pos}, timeout={timeout_pos}, connect={connect_pos}")
        sys.exit(1)
        
    print("Static check PASSED: secureClient.setTimeout(3000) is correctly placed between stop() and connect().")
    sys.exit(0)

if __name__ == '__main__':
    check_timeouts(sys.argv[1])

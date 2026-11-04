import unittest

HOLD_THRESHOLD_MS = 500
ATTEMPT_QUIET_MS = 5000
SUCCESS_QUIET_MS = 3000
PASSWORD_MAX_STEPS = 12
PASSWORD_MIN_STEPS = 4

class ESPStateMachine:
    def __init__(self):
        self.passwordLength = 4
        self.password = ['C', 'C', 'H', 'C']
        self.locked = 0
        self.failedAttempts = 0
        
        self.attemptLength = 0
        self.attemptBuffer = []
        self.lastPhysicalEventMs = -ATTEMPT_QUIET_MS
        self.physicalSuccessPending = False
        self.relayPressed = False
        self.relayOffTime = 0
        
        self.isrTimeQueue = []
        self.isrStateQueue = []
        self.isrQueueOverflow = False
        
        self.buttonLastChangeMs = 0
        self.buttonStableState = 1 # HIGH
        self.buttonIsPressed = False
        self.buttonPressedAtMs = 0
        
        self.events = []
        self.physicalTestMode = False
        self.physicalTestStartedMs = 0
        
    def resetPhysicalAttempt(self):
        self.attemptLength = 0
        self.attemptBuffer = []
        self.physicalSuccessPending = False

    def failPhysicalAttempt(self):
        self.resetPhysicalAttempt()
        self.failedAttempts += 1
        if self.failedAttempts >= 3:
            self.failedAttempts = 3
            self.locked = 1
            self.events.append("locked")
        else:
            self.events.append("failed")

    def processPhysicalSymbol(self, symbol, evtTime):
        if self.physicalTestMode:
            return

        if self.passwordLength < PASSWORD_MIN_STEPS or self.locked:
            return

        if self.attemptLength == 0 and (evtTime - self.lastPhysicalEventMs < ATTEMPT_QUIET_MS):
            self.lastPhysicalEventMs = evtTime
            return

        if self.physicalSuccessPending:
            self.failPhysicalAttempt()
            self.lastPhysicalEventMs = evtTime
            return

        if self.attemptLength < self.passwordLength:
            self.attemptBuffer.append(symbol)
            self.attemptLength += 1
            self.lastPhysicalEventMs = evtTime

        if self.attemptLength == self.passwordLength:
            match = True
            for i in range(self.passwordLength):
                if self.attemptBuffer[i] != self.password[i]:
                    match = False
                    break
            
            if match:
                self.physicalSuccessPending = True
            else:
                self.failPhysicalAttempt()

    def updatePhysicalAttempt(self, nowMs):
        if self.physicalTestMode:
            if nowMs - self.physicalTestStartedMs >= 120000:
                self.physicalTestMode = False
            return

        if self.physicalSuccessPending and nowMs - self.lastPhysicalEventMs >= SUCCESS_QUIET_MS:
            self.resetPhysicalAttempt()
            self.failedAttempts = 0
            self.events.append("success")
            self.relayPressed = True
            self.relayOffTime = nowMs + 500
        elif self.attemptLength > 0 and not self.physicalSuccessPending and nowMs - self.lastPhysicalEventMs >= ATTEMPT_QUIET_MS:
            # NEW: incomplete sequence times out -> fail
            self.failPhysicalAttempt()

    def hardware_interrupt(self, nowMs, rawState):
        if len(self.isrTimeQueue) < 16:
            self.isrTimeQueue.append(nowMs)
            self.isrStateQueue.append(rawState)
        else:
            self.isrQueueOverflow = True
            
    def loop(self, nowMs):
        if self.relayOffTime != 0 and nowMs >= self.relayOffTime:
            self.relayPressed = False
            self.relayOffTime = 0
            
        if self.isrQueueOverflow:
            # NEW: atomically clear queue and state
            self.isrQueueOverflow = False
            self.isrTimeQueue = []
            self.isrStateQueue = []
            self.buttonIsPressed = False
            self.resetPhysicalAttempt()
            self.events.append("queue_overflow")
            
        while len(self.isrTimeQueue) > 0:
            evtTime = self.isrTimeQueue.pop(0)
            evtState = self.isrStateQueue.pop(0)
            
            if evtTime - self.buttonLastChangeMs >= 35:
                if evtState != self.buttonStableState:
                    self.buttonStableState = evtState
                    self.buttonLastChangeMs = evtTime
                    
                    if self.buttonStableState == 0:
                        self.buttonIsPressed = True
                        self.buttonPressedAtMs = evtTime
                    else:
                        if self.buttonIsPressed:
                            self.buttonIsPressed = False
                            durationMs = evtTime - self.buttonPressedAtMs
                            symbol = 'H' if durationMs >= HOLD_THRESHOLD_MS else 'C'
                            self.events.append(f"press_{symbol}")
                            self.processPhysicalSymbol(symbol, evtTime)
                            
        self.updatePhysicalAttempt(nowMs)

class TestESPLogic(unittest.TestCase):
    def test_normal_password(self):
        esp = ESPStateMachine()
        esp.hardware_interrupt(6000, 0)
        esp.hardware_interrupt(6100, 1) # C
        esp.hardware_interrupt(6500, 0)
        esp.hardware_interrupt(6600, 1) # C
        esp.hardware_interrupt(7000, 0)
        esp.hardware_interrupt(7600, 1) # H
        esp.hardware_interrupt(8000, 0)
        esp.hardware_interrupt(8100, 1) # C
        esp.loop(8150)
        self.assertTrue(esp.physicalSuccessPending)
        self.assertFalse(esp.relayPressed)
        esp.loop(11100) # 8100 + 3000
        self.assertTrue(esp.relayPressed)
        self.assertEqual(esp.events[-1], "success")

    def test_no_silence(self):
        esp = ESPStateMachine()
        esp.lastPhysicalEventMs = 5000
        esp.hardware_interrupt(6000, 0)
        esp.hardware_interrupt(6100, 1)
        esp.loop(6150)
        self.assertEqual(esp.attemptLength, 0)
        
    def test_extra_press_cancels_success(self):
        esp = ESPStateMachine()
        esp.hardware_interrupt(6000, 0)
        esp.hardware_interrupt(6100, 1)
        esp.hardware_interrupt(6500, 0)
        esp.hardware_interrupt(6600, 1)
        esp.hardware_interrupt(7000, 0)
        esp.hardware_interrupt(7600, 1)
        esp.hardware_interrupt(8000, 0)
        esp.hardware_interrupt(8100, 1)
        esp.loop(8150)
        self.assertTrue(esp.physicalSuccessPending)
        
        esp.hardware_interrupt(9000, 0)
        esp.hardware_interrupt(9100, 1)
        esp.loop(9150)
        self.assertFalse(esp.physicalSuccessPending)
        self.assertIn("failed", esp.events)
        
    def test_three_fails_lock(self):
        esp = ESPStateMachine()
        for i in range(3):
            now = 10000 + i * 10000
            esp.hardware_interrupt(now, 0)
            esp.hardware_interrupt(now+100, 1)
            esp.hardware_interrupt(now+500, 0)
            esp.hardware_interrupt(now+600, 1)
            esp.hardware_interrupt(now+1000, 0)
            esp.hardware_interrupt(now+1100, 1)
            esp.hardware_interrupt(now+1500, 0)
            esp.hardware_interrupt(now+1600, 1)
            esp.loop(now+2000)
            
        self.assertEqual(esp.locked, 1)
        self.assertIn("locked", esp.events)
        
    def test_three_incomplete_attempts_lock(self):
        esp = ESPStateMachine()
        for i in range(3):
            now = 10000 + i * 10000
            esp.hardware_interrupt(now, 0)
            esp.hardware_interrupt(now+100, 1) # Just 1 press
            esp.loop(now+200)
            self.assertEqual(esp.attemptLength, 1)
            esp.loop(now + ATTEMPT_QUIET_MS + 200) # Time out!
            self.assertEqual(esp.attemptLength, 0)
            
        self.assertEqual(esp.locked, 1)
        self.assertIn("locked", esp.events)
        
    def test_queue_overflow(self):
        esp = ESPStateMachine()
        esp.hardware_interrupt(6000, 0)
        esp.hardware_interrupt(6100, 1)
        esp.loop(6150)
        self.assertEqual(esp.attemptLength, 1)
        
        # overflow
        for i in range(20):
            esp.hardware_interrupt(6200 + i*10, 0)
            
        esp.loop(7000)
        self.assertIn("queue_overflow", esp.events)
        # Attempt must be reset
        self.assertEqual(esp.attemptLength, 0)
        # Symbols must NOT be added (so no success)
        self.assertFalse(esp.physicalSuccessPending)
        self.assertFalse(esp.relayPressed)
        self.assertEqual(len(esp.isrTimeQueue), 0)

    def test_physical_test_mode_bypasses_morse_and_avoids_lock(self):
        esp = ESPStateMachine()
        esp.physicalTestMode = True
        esp.physicalTestStartedMs = 10000

        # In test mode, enter multiple clicks (which would normally fail and lock out after 3)
        for i in range(10):
            now = 10000 + i * 2000
            esp.hardware_interrupt(now, 0)
            esp.hardware_interrupt(now + 100, 1)
            esp.loop(now + 200)
            self.assertEqual(esp.attemptLength, 0)
            esp.loop(now + ATTEMPT_QUIET_MS + 200)
            self.assertEqual(esp.failedAttempts, 0)
            self.assertEqual(esp.locked, 0)
            self.assertFalse(esp.relayPressed)

        self.assertNotIn("locked", esp.events)
        self.assertNotIn("failed", esp.events)
        self.assertNotIn("success", esp.events)

    def test_physical_test_mode_auto_timeout(self):
        esp = ESPStateMachine()
        esp.physicalTestMode = True
        esp.physicalTestStartedMs = 1000
        # Check before timeout
        esp.loop(50000)
        self.assertTrue(esp.physicalTestMode)
        # Check after timeout (>= 120s)
        esp.loop(122000)
        self.assertFalse(esp.physicalTestMode)

if __name__ == '__main__':
    unittest.main()

/*
 * ============================================================================
 * SafeKids / PC-Control: BIO-FINGER IMPEDANCE DETECTOR v2.0 (High-Sensitivity)
 * Безошибочный детектор пальцев: гибрид 50 Гц наводки + RC наносекундных циклов
 * 
 * Пины:
 *   D5 (GPIO14) -> Щуп (касаемся пальцами)
 *   D6 (GPIO12) -> Опорный GND (держим в руке / ладони)
 *   D4 (GPIO2)  -> Встроенный LED
 *   D1 (GPIO5)  -> Реле
 * ============================================================================
 */

#include <Arduino.h>

const int PIN_PROBE = D5; // GPIO14
const int PIN_REF   = D6; // GPIO12
const int PIN_RELAY = D1; // GPIO5
const int PIN_LED   = D4; // GPIO2

// Быстрый замер наводки 50 Гц (High-Z на 20 мс)
int measureHum(int &highCount, int &totalSamples) {
  pinMode(PIN_PROBE, INPUT);
  int transitions = 0;
  highCount = 0;
  totalSamples = 0;

  bool lastState = digitalRead(PIN_PROBE);
  unsigned long start = micros();

  while (micros() - start < 20000) { // 20 мс = 1 полный период 50 Гц
    bool s = digitalRead(PIN_PROBE);
    if (s) highCount++;
    totalSamples++;

    if (s != lastState) {
      transitions++;
      lastState = s;
    }
    delayMicroseconds(50);
  }
  return transitions;
}

// Замер времени заряда через pullup (в циклах CPU)
uint32_t measureRiseCycles() {
  pinMode(PIN_PROBE, OUTPUT);
  digitalWrite(PIN_PROBE, LOW);
  delayMicroseconds(30);

  uint32_t startCycles = ESP.getCycleCount();
  pinMode(PIN_PROBE, INPUT_PULLUP);

  while (digitalRead(PIN_PROBE) == LOW) {
    if ((ESP.getCycleCount() - startCycles) > 800000) { // 10 мс таймаут
      break;
    }
  }

  return (ESP.getCycleCount() - startCycles);
}

// Замер времени спада заряда (RC decay в микросекундах)
uint32_t measureDecayMicros() {
  pinMode(PIN_PROBE, OUTPUT);
  digitalWrite(PIN_PROBE, HIGH);
  delayMicroseconds(30);

  unsigned long t0 = micros();
  pinMode(PIN_PROBE, INPUT); // High-Z разряд через палец в D6 (GND)

  while (digitalRead(PIN_PROBE) == HIGH) {
    if (micros() - t0 > 15000) break; // 15 мс таймаут
  }

  return (micros() - t0);
}

void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(PIN_REF, OUTPUT);
  digitalWrite(PIN_REF, LOW); // Опорный контакт в GND

  pinMode(PIN_RELAY, OUTPUT);
  digitalWrite(PIN_RELAY, HIGH); // Реле выключено

  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, HIGH);   // LED выключен

  Serial.println("\n{\"system\":\"READY\",\"version\":\"2.0\"}");
}

unsigned long lastSend = 0;
bool wasTouched = false;

void loop() {
  int highCount = 0;
  int totalSamples = 0;

  // 1. Слушаем 50 Гц наводку тела
  int humTransitions = measureHum(highCount, totalSamples);

  // 2. Делаем серию замеров RC
  uint32_t cycles = measureRiseCycles();
  uint32_t decayUs = measureDecayMicros();

  // Касание фиксируется, если есть сетевая наводка тела (>=2 переходов)
  // ИЛИ если емкость/сопротивление пальца изменили такты (>150 циклов или >20 мкс спада)
  bool isTouched = (humTransitions >= 2) || (cycles > 150) || (decayUs > 15);

  digitalWrite(PIN_LED, isTouched ? LOW : HIGH);

  unsigned long now = millis();

  if (isTouched) {
    if (!wasTouched || now - lastSend >= 150) {
      wasTouched = true;
      lastSend = now;

      float usRise = (float)cycles / 80.0f;
      float duty = totalSamples > 0 ? ((float)highCount / totalSamples) * 100.0f : 0.0f;

      // Шлем телеметрию в JSON
      Serial.printf("{\"event\":\"TOUCH\",\"touch\":true,\"cycles\":%u,\"us_rise\":%.1f,\"decay_us\":%u,\"hum\":%d,\"duty\":%.0f}\n",
                    cycles, usRise, decayUs, humTransitions, duty);
    }
  } else {
    if (wasTouched || now - lastSend >= 800) {
      wasTouched = false;
      lastSend = now;

      Serial.println("{\"event\":\"IDLE\",\"touch\":false}");
    }
  }

  delay(15);
}

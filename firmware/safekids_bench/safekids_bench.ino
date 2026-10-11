/*
 * SafeKids Socket - Anti-Hacker Scissors & Human Bio-Detector v3.0
 * Пины: D5 (Probe), D6 (GND Ref), D1 (Relay), D4 (Builtin LED)
 */

const int PIN_RELAY       = D1;  // Силовое реле / лампа (Active LOW)
const int PIN_BUILTIN_LED = D4;  // Синий светодиод на плате (Active LOW)
const int PIN_PROBE       = D5;  // Сенсорный щуп (Провод 1)
const int PIN_GND_REF     = D6;  // Опорный контакт (Провод 2)

enum ObjectType {
  OBJ_AIR,       // Пустота / воздух
  OBJ_METAL,     // Чистый металл БЕЗ человека (ножницы на столе / вилка)
  OBJ_HUMAN      // ЧЕЛОВЕК! (касается ножниц, провода или держит металл)
};

ObjectType detectedType = OBJ_AIR;
unsigned long lastLogTime = 0;

ObjectType analyzeSensor() {
  // =========================================================================
  // ШАГ 1: ПЕРВЫЙ И ГЛАВНЫЙ ФИЛЬТР — СЛУШАЕМ ТЕЛО ЧЕЛОВЕКА (50 Гц наводка)
  // Переводим ОБА пина в свободный высокоомный режим (High-Z) без подтяжек
  // =========================================================================
  pinMode(PIN_PROBE, INPUT);
  pinMode(PIN_GND_REF, INPUT);

  int transitions = 0;
  bool lastState5 = digitalRead(PIN_PROBE);
  bool lastState6 = digitalRead(PIN_GND_REF);
  unsigned long startT = micros();

  // Слушаем 25 миллисекунд (больше одного полного периода сетевой наводки 50 Гц)
  while (micros() - startT < 25000) {
    bool s5 = digitalRead(PIN_PROBE);
    bool s6 = digitalRead(PIN_GND_REF);

    if (s5 != lastState5) { transitions++; lastState5 = s5; }
    if (s6 != lastState6) { transitions++; lastState6 = s6; }
    delayMicroseconds(50);
  }

  // Если пины колеблются от наводки человека — ЭТО РУКА ХАКЕРА!
  if (transitions >= 3) {
    return OBJ_HUMAN;
  }

  // Дополнительный тест: RC-емкость живого тела
  pinMode(PIN_PROBE, OUTPUT);
  digitalWrite(PIN_PROBE, HIGH);
  delayMicroseconds(20);
  pinMode(PIN_PROBE, INPUT);
  delayMicroseconds(100);
  if (digitalRead(PIN_PROBE) == LOW) {
    // Емкость кожи разрядила щуп
    return OBJ_HUMAN;
  }

  // =========================================================================
  // ШАГ 2: ЕСЛИ ЧЕЛОВЕК НЕ ДЕРЖИТ МЕТАЛЛ — ПРОВЕРЯЕМ ЗАМЫКАНИЕ
  // =========================================================================
  pinMode(PIN_GND_REF, OUTPUT);
  digitalWrite(PIN_GND_REF, LOW);

  pinMode(PIN_PROBE, INPUT_PULLUP);
  delayMicroseconds(50);

  if (digitalRead(PIN_PROBE) == LOW) {
    // Чистый металл (ножницы лежат на столе без рук человека, или фабричная вилка)
    return OBJ_METAL;
  }

  return OBJ_AIR;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=======================================================");
  Serial.println("  SafeKids Anti-Hacker: ДЕТЕКТОР НОЖНИЦ И ПАЛЬЦЕВ v3.0 ");
  Serial.println("=======================================================");

  pinMode(PIN_RELAY, OUTPUT);
  digitalWrite(PIN_RELAY, HIGH); // Реле выключено

  pinMode(PIN_BUILTIN_LED, OUTPUT);
  digitalWrite(PIN_BUILTIN_LED, HIGH); // Светодиод выключен
}

void loop() {
  unsigned long now = millis();
  ObjectType current = analyzeSensor();

  if (current != detectedType || now - lastLogTime > 500) {
    detectedType = current;
    lastLogTime = now;

    if (detectedType == OBJ_METAL) {
      // Чистый металл БЕЗ рук: реле ВКЛЮЧЕНО, светодиод ГОРИТ ровно!
      digitalWrite(PIN_RELAY, LOW);
      digitalWrite(PIN_BUILTIN_LED, LOW);
      Serial.println("[МЕТАЛЛ] Чистый металл БЕЗ человека. РЕЛЕ ВКЛЮЧЕНО! Лампа горит.");
    } 
    else if (detectedType == OBJ_HUMAN) {
      // ХАКЕР ДЕРЖИТ НОЖНИЦЫ: РЕЛЕ МГНОВЕННО ВЫРУБИТЬ, ДИОД МИГАЕТ ТРЕВОГУ!
      digitalWrite(PIN_RELAY, HIGH);
      for (int i = 0; i < 4; i++) {
        digitalWrite(PIN_BUILTIN_LED, LOW);
        delay(40);
        digitalWrite(PIN_BUILTIN_LED, HIGH);
        delay(40);
      }
      Serial.println("[ХАКЕР ПОЙМАН!] Человек касается ножниц/провода! РЕЛЕ ВЫКЛЮЧЕНО! ТРЕВОГА!");
    } 
    else {
      // Покой (воздух)
      digitalWrite(PIN_RELAY, HIGH);
      digitalWrite(PIN_BUILTIN_LED, HIGH);
    }
  }

  delay(20);
}

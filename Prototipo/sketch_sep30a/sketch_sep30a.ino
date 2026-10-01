#include <LiquidCrystal.h>

LiquidCrystal lcd(12, 11, 5, 4, 3, 6);  // RS, E, D4, D5, D6, D7

void mostrarKills(long kills) {
  lcd.setCursor(0, 1);
  lcd.print("                ");  // apaga a linha inteira
  lcd.setCursor(0, 1);
  lcd.print(kills);
}

void setup() {
  Serial.begin(9600);
  lcd.begin(16, 2);
  lcd.print("KILLS");
  mostrarKills(0);
}

void loop() {
  if (Serial.available() > 0) {
    String linha = Serial.readStringUntil('\n');
    linha.trim();
    if (linha.length() > 0) {
      mostrarKills(linha.toInt());
    }
  }
}

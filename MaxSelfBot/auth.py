import asyncio
import io
import cv2
import numpy as np
import qrcode
from PIL import Image
from pyzbar.pyzbar import decode
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

async def process_and_print_qr(page: Page):
    try:
        # Вместо поиска тегов просто даем странице 2-3 секунды "продышаться"
        # чтобы QR-код успел отрисоваться во фреймворке (Svelte)
        await asyncio.sleep(2) 
        
        print("Снимаю скриншот для обработки QR...")
        screenshot_bytes = await page.screenshot()
        img = Image.open(io.BytesIO(screenshot_bytes))
        
        # Твои координаты
        x, y, w, h = 522, 177, 234, 234
        cropped_img = img.crop((x, y, x + w, y + h))
        
        cv_img = np.array(cropped_img)
        cv_img = cv2.cvtColor(cv_img, cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
        
        decoded_objects = decode(gray)
        if not decoded_objects:
            # Усиленная бинаризация для "стилизованных" кодов
            _, thresh = cv2.threshold(gray, 128, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
            decoded_objects = decode(thresh)

        if decoded_objects:
            qr_text = decoded_objects[0].data.decode('utf-8')
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(qr_text)
            qr.make(fit=True)
            print("\n[СКАНИРУЙТЕ QR-КОД]")
            qr.print_ascii(tty=True)
            return True
        else:
            print("[!] QR-код на скриншоте не обнаружен. Проверь координаты или масштаб страницы.")
            # Для отладки можно сохранить скриншот, чтобы увидеть, что видит бот
            # await page.screenshot(path="debug_screen.png") 
            return False
            
    except Exception as e:
        print(f"Ошибка при обработке QR: {e}")
        return False

async def run_auth(page: Page):
    print("Запуск процесса авторизации...")
    await page.goto("https://web.max.ru", wait_until="networkidle")
    
    await process_and_print_qr(page)

    refresh_sel = 'button[aria-label="Обновить QR-код"]'
    password_sel = 'input[type="password"]'
    chats_sel = '#aside-header-title'
    combined_selector = f"{refresh_sel}, {password_sel}, {chats_sel}"

    while True:
        try:
            await page.wait_for_selector(combined_selector, timeout=5000)
            
            if await page.locator(chats_sel).is_visible():
                print("Авторизация успешна!")
                return True
            
            if await page.locator(password_sel).is_visible():
                password = await asyncio.to_thread(input, "Введите облачный пароль: ")
                await page.locator(password_sel).fill(password)
                await page.locator('button[aria-label="Продолжить"]').click()
                await page.wait_for_selector(chats_sel, timeout=10000)
                return True

            if await page.locator(refresh_sel).is_visible():
                await page.locator(refresh_sel).click()
                await asyncio.sleep(2)
                await process_and_print_qr(page)
                
        except PlaywrightTimeoutError:
            continue
#!/usr/bin/env python3
"""
測試資料庫連接
使用方式: python3 test_db_connection.py
"""

import os
from dotenv import load_dotenv
import mysql.connector
from mysql.connector import Error

load_dotenv()

DB_CONFIG = {
    'host': os.getenv("DB_HOST", "localhost"),
    'user': os.getenv("DB_USER", "admin"),
    'password': os.getenv("DB_PASSWORD", "960404"),
    'database': os.getenv("DB_NAME", "tutoring_db"),
    'charset': 'utf8mb4'
}

def test_connection():
    """測試資料庫連接"""
    print("🔧 測試 MariaDB 連接...\n")
    print(f"連接設定:")
    print(f"  主機: {DB_CONFIG['host']}")
    print(f"  使用者: {DB_CONFIG['user']}")
    print(f"  資料庫: {DB_CONFIG['database']}\n")
    
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        
        if connection.is_connected():
            db_info = connection.get_server_info()
            print(f"✅ 成功連接到 MariaDB/MySQL")
            print(f"   版本: {db_info}\n")
            
            cursor = connection.cursor()
            
            # 測試查詢
            print("📊 查詢課程數量...")
            cursor.execute("SELECT COUNT(*) as count FROM schedule")
            result = cursor.fetchone()
            count = result[0] if result else 0
            print(f"   資料庫中有 {count} 堂課程\n")
            
            # 列出最近的課程
            if count > 0:
                print("📅 最近的 5 堂課程:")
                cursor.execute("""
                    SELECT date, time, title, teacher 
                    FROM schedule 
                    ORDER BY date, time 
                    LIMIT 5
                """)
                
                for idx, (date, time, title, teacher) in enumerate(cursor.fetchall(), 1):
                    print(f"   {idx}. {date} {time} - {title} (講師: {teacher})")
            else:
                print("⚠️  資料庫中尚無課程\n")
            
            cursor.close()
            connection.close()
            
            print("\n✅ 連接測試完成！")
            return True
            
    except Error as e:
        print(f"❌ 連接失敗!")
        print(f"   錯誤訊息: {e}\n")
        
        print("💡 故障排除建議:")
        print(f"   1. 檢查主機 IP: {DB_CONFIG['host']}")
        print(f"   2. 檢查 MariaDB 是否在樹莓派上運行")
        print(f"   3. 檢查防火牆設定（port 3306）")
        print(f"   4. 驗證 .env 中的設定是否正確")
        print(f"   5. 嘗試在樹莓派上直接連接: mysql -u {DB_CONFIG['user']} -p\n")
        
        return False

if __name__ == '__main__':
    success = test_connection()
    exit(0 if success else 1)

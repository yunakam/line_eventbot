# line_eventbot
LIFFで動作するLINEグループ向けイベント作成・管理アプリ

## 構成
- フロントエンド: LIFF (LINE Front-end Framework)  
  - イベント作成・参加・キャンセルなどをUIで操作  
- バックエンド: Django  
  - イベントデータ管理、ユーザー操作の処理  

## 動作検証
### Backend (Django) の起動
`python manage.py runserver`

### ngrokで公開
`ngrok http 8000`
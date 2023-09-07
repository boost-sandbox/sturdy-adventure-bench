set -ex

cd /src/src

dotnet restore NopCommerce.sln

cd /src/src/Presentation/Nop.Web

dotnet build Nop.Web.csproj -c Release

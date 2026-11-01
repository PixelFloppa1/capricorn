-- Capri Player: только ЧИТАЕТ состояние игры и пишет строку в log.txt. Геймплей не меняет.
-- Формат строки (разбирает isaac.py):
-- CAPRI f=<кадр> room=<индекс> clear=0/1 boss=0/1 st=<этаж> p=x,y hp=<красные+души> bombs=<n> dead=0/1
--   E=x,y,hp;...  P=x,y,vx,vy;...  D=слот,x,y,открыта,комната;...  K=x,y,цена;...  T=x,y
local mod = RegisterMod("Capri Player", 1)
local EVERY = 4              -- раз в столько кадров (игра идёт 30 кадров/с)
local seen_dead = false

local function r(v) return math.floor(v + 0.5) end

local function door_open(d)
  local ok, res = pcall(function() return d:IsOpen() end)
  if ok and res ~= nil then return res end
  local ok2, st = pcall(function() return d.State end)
  return ok2 and st == 2
end

function mod:OnUpdate()
  local game = Game()
  if game:GetFrameCount() % EVERY ~= 0 then return end
  local level, room = game:GetLevel(), game:GetRoom()
  local player = Isaac.GetPlayer(0)
  if player == nil then return end

  local p = player.Position
  local hp = player:GetHearts() + player:GetSoulHearts()
  local dead = (player:IsDead() or hp <= 0) and 1 or 0
  if dead == 0 then seen_dead = false end
  if dead == 1 and seen_dead then return end   -- смерть пишем один раз
  if dead == 1 then seen_dead = true end

  local enemies, shots, kinds = {}, {}, {}
  for _, e in ipairs(Isaac.GetRoomEntities()) do
    if e.Type == EntityType.ENTITY_PROJECTILE then
      shots[#shots + 1] = string.format("%d,%d,%d,%d", r(e.Position.X), r(e.Position.Y), r(e.Velocity.X), r(e.Velocity.Y))
    elseif e:IsActiveEnemy(false) and e:IsVulnerableEnemy() and not e:HasEntityFlags(EntityFlag.FLAG_FRIENDLY) then
      enemies[#enemies + 1] = string.format("%d,%d,%d", r(e.Position.X), r(e.Position.Y), r(e.HitPoints))
    elseif e.Type == EntityType.ENTITY_PICKUP and e.Variant == PickupVariant.PICKUP_COLLECTIBLE and e.SubType ~= 0 then
      local pk = e:ToPickup()
      kinds[#kinds + 1] = string.format("%d,%d,%d", r(e.Position.X), r(e.Position.Y), pk and pk.Price or 0)
    end
  end

  local doors = {}
  for slot = 0, 7 do
    local d = room:GetDoor(slot)
    if d ~= nil then
      doors[#doors + 1] = string.format("%d,%d,%d,%d,%d", slot, r(d.Position.X), r(d.Position.Y),
        door_open(d) and 1 or 0, d.TargetRoomIndex or -1)
    end
  end

  local trap = ""
  if room:IsClear() and game:GetFrameCount() % (EVERY * 8) == 0 then
    for i = 0, room:GetGridSize() - 1 do
      local g = room:GetGridEntity(i)
      if g ~= nil and g:GetType() == GridEntityType.GRID_TRAPDOOR then
        local pos = room:GetGridPosition(i)
        trap = string.format("%d,%d", r(pos.X), r(pos.Y))
        break
      end
    end
  end

  Isaac.DebugString(string.format(
    "CAPRI f=%d room=%d clear=%d boss=%d st=%d p=%d,%d hp=%d bombs=%d dead=%d E=%s P=%s D=%s K=%s T=%s",
    game:GetFrameCount(), level:GetCurrentRoomIndex(), room:IsClear() and 1 or 0,
    room:GetType() == RoomType.ROOM_BOSS and 1 or 0, level:GetStage(), r(p.X), r(p.Y), hp,
    player:GetNumBombs(), dead, table.concat(enemies, ";"), table.concat(shots, ";"),
    table.concat(doors, ";"), table.concat(kinds, ";"), trap))
end

mod:AddCallback(ModCallbacks.MC_POST_UPDATE, mod.OnUpdate)
